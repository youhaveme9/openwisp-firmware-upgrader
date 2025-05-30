import logging
import os
from decimal import Decimal

import jsonschema
import swapper
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from private_storage.fields import PrivateFileField

from openwisp_controller.connection.exceptions import NoWorkingDeviceConnectionError
from openwisp_users.mixins import ShareableOrgMixin
from openwisp_utils.base import TimeStampedEditableModel

from .. import settings as app_settings
from ..exceptions import (
    FirmwareUpgradeOptionsException,
    ReconnectionFailed,
    RecoverableFailure,
    UpgradeAborted,
    UpgradeNotNeeded,
)
from ..hardware import (
    FIRMWARE_IMAGE_MAP,
    FIRMWARE_IMAGE_TYPE_CHOICES,
    REVERSE_FIRMWARE_IMAGE_MAP,
)
from ..swapper import get_model_name, load_model
from ..tasks import (
    batch_upgrade_operation,
    create_all_device_firmwares,
    create_device_firmware,
    upgrade_firmware,
)
from ..utils import (
    get_upgrader_class_for_device,
    get_upgrader_class_from_device_connection,
    get_upgrader_schema_for_device,
)

logger = logging.getLogger(__name__)


class UpgradeOptionsMixin(models.Model):
    upgrade_options = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True

    def validate_upgrade_options(self):
        if not self.upgrade_options:
            return
        if not getattr(self.upgrader_class, "SCHEMA"):
            raise ValidationError(
                _("Using upgrade options is not allowed with this upgrader.")
            )
        try:
            self.upgrader_class.validate_upgrade_options(self.upgrade_options)
        except jsonschema.ValidationError:
            raise ValidationError("The upgrade options are invalid")
        except FirmwareUpgradeOptionsException as error:
            raise ValidationError(*error.args)

    def clean(self):
        super().clean()
        self.validate_upgrade_options()


class AbstractCategory(ShareableOrgMixin, TimeStampedEditableModel):
    name = models.CharField(max_length=64, db_index=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name

    class Meta:
        abstract = True
        verbose_name = _("Firmware Category")
        verbose_name_plural = _("Firmware Categories")
        unique_together = ("name", "organization")


class AbstractBuild(TimeStampedEditableModel):
    category = models.ForeignKey(
        get_model_name("Category"),
        on_delete=models.CASCADE,
        verbose_name=_("firmware category"),
        help_text=_(
            "if you have different firmware types "
            "eg: (BGP routers, wifi APs, DSL gateways) "
            "create a category for each."
        ),
    )
    version = models.CharField(max_length=32, db_index=True)
    os = models.CharField(
        _("OS identifier"),
        max_length=64,
        blank=True,
        null=True,
        help_text=_(
            "OS identifier as presented by the device, "
            "used to automatically recognize the firmware "
            "image used by new devices that register "
            "into the system"
        ),
    )
    changelog = models.TextField(
        _("change log"),
        blank=True,
        help_text=_(
            "descriptive text indicating what "
            "has changed since the previous "
            "version, if applicable"
        ),
    )

    class Meta:
        abstract = True
        verbose_name = _("Firmware Build")
        verbose_name_plural = _("Firmware Builds")
        unique_together = ("category", "version")
        ordering = ("-created",)

    def __str__(self):
        try:
            return f"{self.category} v{self.version}"
        except ObjectDoesNotExist:
            return super().__str__()

    def clean(self):
        # Make sure that ('category__organization', 'os') is unique too
        try:
            category = self.category
        except ObjectDoesNotExist:
            return
        if not self.os:
            return
        if (
            load_model("Build")
            .objects.filter(category__organization=category.organization, os=self.os)
            .exclude(pk=self.pk)
            .exists()
        ):
            raise ValidationError(
                {
                    "os": _(
                        f'A build with this OS identifier ("{self.os}") and '
                        f'organization ("{category.organization}") already exists'
                    )
                }
            )

    def batch_upgrade(self, firmwareless, upgrade_options=None):
        upgrade_options = upgrade_options or {}
        batch = load_model("BatchUpgradeOperation")(
            build=self, upgrade_options=upgrade_options
        )
        batch.full_clean()
        batch.save()
        transaction.on_commit(
            lambda: batch_upgrade_operation.delay(batch.pk, firmwareless)
        )
        return batch

    def _find_related_device_firmwares(self, select_devices=False):
        """
        Returns all the DeviceFirmware objects related to the firmware
        category of this build that have not been installed yet
        """
        related = ["image"]
        if select_devices:
            related.append("device")
        return (
            load_model("DeviceFirmware")
            .objects.all()
            .select_related(*related)
            .filter(image__build__category_id=self.category_id)
            .exclude(image__build=self, installed=True)
            .order_by("-created")
        )

    def _find_firmwareless_devices(self, boards=None):
        """
        Returns devices which have no related DeviceFirmware
        but that are upgradable to one of the image of this build
        """
        if boards is None:
            boards = []
            for image in self.firmwareimage_set.all():
                boards += image.boards
        Device = swapper.load_model("config", "Device")
        qs = Device.objects.filter(
            devicefirmware__isnull=True,
            model__in=boards,
        )
        if self.category.organization_id:
            qs = qs.filter(organization_id=self.category.organization_id)
        return qs.order_by("-created")


def get_build_directory(instance, filename):
    build_pk = str(instance.build.pk)
    return "/".join([build_pk, filename])


class AbstractFirmwareImage(TimeStampedEditableModel):
    build = models.ForeignKey(get_model_name("Build"), on_delete=models.CASCADE)
    file = PrivateFileField(
        "File",
        upload_to=get_build_directory,
        max_file_size=app_settings.MAX_FILE_SIZE,
        storage=app_settings.PRIVATE_STORAGE_INSTANCE,
        max_length=255,
    )
    type = models.CharField(
        blank=True,
        max_length=128,
        choices=FIRMWARE_IMAGE_TYPE_CHOICES,
        help_text=_(
            "firmware image type: model or "
            "architecture. Leave blank to attempt "
            "determining automatically"
        ),
    )

    class Meta:
        abstract = True
        verbose_name = _("Firmware Image")
        verbose_name_plural = _("Firmware Images")
        unique_together = ("build", "type")

    def __str__(self):
        if hasattr(self, "build") and self.type:
            return f"{self.build}: {self.get_type_display()}"
        return super().__str__()

    @property
    def boards(self):
        return FIRMWARE_IMAGE_MAP[self.type]["boards"]

    def clean(self):
        self._clean_type()
        try:
            self.boards
        except KeyError:
            raise ValidationError({"type": "Could not find boards for this type"})

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        self._remove_file()

    def _remove_file(self):
        firmware_filename = self.file.name
        self.file.storage.delete(firmware_filename)
        # Delete the file directory
        dir = os.path.split(firmware_filename)[0]
        if dir:
            try:
                self.file.storage.delete(dir)
            except OSError as error:
                # A build can have multiple firmware images,
                # therefore, deleting directory might not be
                # always feasible.
                if "Directory not empty" not in str(error):
                    raise error

    def _clean_type(self):
        """
        auto determine type if missing
        """
        if self.type:
            return
        filename = self.file.name
        # removes leading prefix
        self.type = "-".join(filename.split("-")[1:])


class AbstractDeviceFirmware(TimeStampedEditableModel):
    device = models.OneToOneField(
        swapper.get_model_name("config", "Device"), on_delete=models.CASCADE
    )
    image = models.ForeignKey(get_model_name("FirmwareImage"), on_delete=models.CASCADE)
    installed = models.BooleanField(default=False)
    _old_image = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._update_old_image()

    class Meta:
        verbose_name = _("Device Firmware")
        abstract = True

    def clean(self):
        if not hasattr(self, "image") or not hasattr(self, "device"):
            return
        if (
            self.image.build.category.organization is not None
            and self.image.build.category.organization != self.device.organization
        ):
            raise ValidationError(
                {
                    "image": _(
                        "The organization of the image doesn't "
                        "match the organization of the device"
                    )
                }
            )
        if self.device.deviceconnection_set.count() < 1:
            raise ValidationError(
                _(
                    "This device does not have a related connection object defined "
                    "yet and therefore it would not be possible to upgrade it, "
                    'please add one in the section named "Credentials"'
                )
            )
        if self.device.model not in self.image.boards:
            raise ValidationError(_("Device model and image model do not match"))

    @property
    def image_has_changed(self):
        return self._state.adding or self.image_id != self._old_image.id

    def save(self, batch=None, upgrade=True, upgrade_options=None, *args, **kwargs):
        # if firwmare image has changed launch upgrade
        # upgrade won't be launched the first time
        if upgrade and (self.image_has_changed or not self.installed):
            self.installed = False
            super().save(*args, **kwargs)
            self.create_upgrade_operation(batch, upgrade_options=upgrade_options or {})
        else:
            super().save(*args, **kwargs)
        self._update_old_image()

    def _update_old_image(self):
        if hasattr(self, "image"):
            self._old_image = self.image

    def create_upgrade_operation(self, batch, upgrade_options=None):
        uo_model = load_model("UpgradeOperation")
        operation = uo_model(
            device=self.device, image=self.image, upgrade_options=upgrade_options
        )
        if batch:
            operation.batch = batch
        operation.full_clean()
        operation.save()
        # launch ``upgrade_firmware`` in the background (celery)
        # once changes are committed to the database
        transaction.on_commit(lambda: upgrade_firmware.delay(operation.pk))
        return operation

    @classmethod
    def create_for_device(cls, device, firmware_image=None):
        """
        Creates a ``DeviceFirmware`` instance for the specified device
        If ``firmware_image`` is not supplied, it will be tried
        to be determined automatically.

        May return ``None`` if it was not possible to create the DeviceFirmware.
        """
        DeviceFirmware = load_model("DeviceFirmware")
        FirmwareImage = load_model("FirmwareImage")
        image_type = REVERSE_FIRMWARE_IMAGE_MAP.get(device.model)

        if not image_type:
            return

        if not firmware_image:
            try:
                firmware_image = FirmwareImage.objects.get(
                    build__category__organization_id=device.organization_id,
                    build__os=device.os,
                    type=image_type,
                )
            except FirmwareImage.DoesNotExist:
                return

        device_fw = DeviceFirmware(device=device, image=firmware_image, installed=True)
        try:
            device_fw.full_clean()
        except ValidationError as e:
            logger.warning(e)
            return
        device_fw.save(upgrade=False)
        return device_fw

    @classmethod
    def auto_add_device_firmware_to_device(cls, instance, created, **kwargs):
        # Automatically associate DeviceFirmware to the registered Device
        if not created:
            return
        if not instance.device.os or not instance.device.model:
            return
        if instance.device.model not in REVERSE_FIRMWARE_IMAGE_MAP:
            return

        transaction.on_commit(lambda: create_device_firmware.delay(instance.device.pk))

    @classmethod
    def auto_create_device_firmwares(cls, instance, created, **kwargs):
        if created:
            transaction.on_commit(
                lambda: create_all_device_firmwares.delay(instance.pk)
            )

    @classmethod
    def get_image_queryset_for_device(cls, device, device_firmware=None):
        FirmwareImage = cls.image.field.related_model
        qs = (
            FirmwareImage.objects.filter(
                Q(build__category__organization_id=device.organization_id)
                | Q(build__category__organization__isnull=True)
            )
            .order_by("-created")
            .select_related("build", "build__category")
        )
        # if device model is defined
        # restrict the images to the ones compatible with it
        if device.model and device.model in REVERSE_FIRMWARE_IMAGE_MAP:
            qs = qs.filter(type=REVERSE_FIRMWARE_IMAGE_MAP[device.model])
        # if DeviceFirmware instance already exists
        # restrict images to the ones of the same category
        if device_firmware and hasattr(device_firmware, "image"):
            qs = qs.filter(build__category_id=device_firmware.image.build.category_id)
        return qs


class AbstractBatchUpgradeOperation(UpgradeOptionsMixin, TimeStampedEditableModel):
    build = models.ForeignKey(get_model_name("Build"), on_delete=models.CASCADE)
    STATUS_CHOICES = (
        ("idle", _("idle")),
        ("in-progress", _("in progress")),
        ("success", _("completed successfully")),
        ("failed", _("completed with some failures")),
    )
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_CHOICES[0][0]
    )

    class Meta:
        abstract = True
        verbose_name = _("Mass upgrade operation")
        verbose_name_plural = _("Mass upgrade operations")

    def __str__(self):
        return f"Upgrade of {self.build} on {self.created}"

    def update(self):
        operations = self.upgradeoperation_set
        if operations.filter(status="in-progress").exists():
            return
        # if there's any failed operation, mark as failure
        if operations.filter(status="failed").exists():
            self.status = "failed"
        else:
            self.status = "success"
        self.save()

    def upgrade(self, firmwareless):
        self.status = "in-progress"
        self.save()
        self.upgrade_related_devices()
        if firmwareless:
            self.upgrade_firmwareless_devices()

    @staticmethod
    def dry_run(build):
        related_device_fw = build._find_related_device_firmwares(select_devices=True)
        firmwareless_devices = build._find_firmwareless_devices()
        return {
            "device_firmwares": related_device_fw,
            "devices": firmwareless_devices,
        }

    def upgrade_related_devices(self):
        """
        upgrades all devices which have an
        existing related DeviceFirmware
        """
        device_firmwares = self.build._find_related_device_firmwares()
        for device_fw in device_firmwares:
            image = self.build.firmwareimage_set.filter(
                type=device_fw.image.type
            ).first()
            if image:
                device_fw.image = image
                device_fw.full_clean()
                device_fw.save(self, upgrade_options=self.upgrade_options)

    def upgrade_firmwareless_devices(self):
        """
        upgrades all devices which do not
        have a related DeviceFirmware yet
        (referred as "firmwareless")
        """
        # for each image, find related "firmwareless"
        # devices and perform upgrade one by one
        for image in self.build.firmwareimage_set.all():
            devices = self.build._find_firmwareless_devices(image.boards)
            for device in devices:
                DeviceFirmware = load_model("DeviceFirmware")
                device_fw = DeviceFirmware(device=device, image=image)
                device_fw.full_clean()
                device_fw.save(self, upgrade_options=self.upgrade_options)

    @cached_property
    def upgrade_operations(self):
        return self.upgradeoperation_set.all()

    @cached_property
    def total_operations(self):
        return self.upgrade_operations.count()

    @property
    def progress_report(self):
        completed = self.upgrade_operations.exclude(status="in-progress").count()
        return _(f"{completed} out of {self.total_operations}")

    @property
    def success_rate(self):
        if not self.total_operations:
            return 0
        success = self.upgrade_operations.filter(status="success").count()
        return self.__get_rate(success)

    @property
    def failed_rate(self):
        if not self.total_operations:
            return 0
        failed = self.upgrade_operations.filter(status="failed").count()
        return self.__get_rate(failed)

    @property
    def aborted_rate(self):
        if not self.total_operations:
            return 0
        aborted = self.upgrade_operations.filter(status="aborted").count()
        return self.__get_rate(aborted)

    @property
    def upgrader_class(self):
        return self._get_upgrader_class()

    @property
    def upgrader_schema(self):
        return self._get_upgrader_schema()

    def _get_upgrader_class(self, related_device_fw=None, firmwareless_devices=None):
        if self.upgrade_operations:
            return get_upgrader_class_for_device(self.upgrade_operations[0].device)
        related_device_fw = (
            related_device_fw
            or self.build._find_related_device_firmwares(select_devices=True)
        )
        if related_device_fw:
            return get_upgrader_class_for_device(related_device_fw.first().device)
        firmwareless_devices = (
            firmwareless_devices or self.build._find_firmwareless_devices()
        )
        if firmwareless_devices:
            return get_upgrader_class_for_device(firmwareless_devices.first())

    def _get_upgrader_schema(self, related_device_fw=None, firmwareless_devices=None):
        upgrader_class = self._get_upgrader_class(
            related_device_fw, firmwareless_devices
        )
        return getattr(upgrader_class, "SCHEMA", None)

    def __get_rate(self, number):
        result = Decimal(number) / Decimal(self.total_operations) * 100
        return round(result, 2)


class AbstractUpgradeOperation(UpgradeOptionsMixin, TimeStampedEditableModel):
    STATUS_CHOICES = (
        ("in-progress", _("in progress")),
        ("success", _("success")),
        ("failed", _("failed")),
        ("aborted", _("aborted")),
    )
    device = models.ForeignKey(
        swapper.get_model_name("config", "Device"), on_delete=models.CASCADE
    )
    image = models.ForeignKey(
        get_model_name("FirmwareImage"), null=True, on_delete=models.SET_NULL
    )
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_CHOICES[0][0]
    )
    log = models.TextField(blank=True)
    batch = models.ForeignKey(
        get_model_name("BatchUpgradeOperation"),
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )

    class Meta:
        abstract = True

    def log_line(self, line, save=True):
        if self.log:
            self.log += f"\n{line}"
        else:
            self.log = line
        logger.info(f"# {line}")
        if save:
            self.save()

    def _recoverable_failure_handler(self, recoverable, error):
        cause = str(error)
        if recoverable:
            self.log_line(f"Detected a recoverable failure: {cause}.\n", save=False)
            self.log_line("The upgrade operation will be retried soon.")
            raise error
        self.status = "failed"
        self.log_line(f"Max retries exceeded. Upgrade failed: {cause}.", save=False)

    def upgrade(self, recoverable=True):
        DeviceConnection = swapper.load_model("connection", "DeviceConnection")
        try:
            conn = DeviceConnection.get_working_connection(self.device)
        except NoWorkingDeviceConnectionError as error:
            if error.connection is None:
                self.log_line("No device connection available")
                return

            log_template = (
                "Failed to connect with device using {credentials}."
                " Error: {failure_reason}"
            )
            for conn in self.device.deviceconnection_set.select_related("credentials"):
                self.log_line(
                    log_template.format(
                        credentials=conn.credentials,
                        failure_reason=conn.failure_reason,
                    ),
                    save=False,
                )
            self._recoverable_failure_handler(
                recoverable,
                RecoverableFailure(
                    (
                        "Failed to establish connection with the device,"
                        " tried all DeviceConnections"
                    )
                ),
            )
            self.save()
            return
        installed = False
        # prevent multiple upgrade operations for
        # the same device running at the same time
        qs = (
            load_model("UpgradeOperation")
            .objects.filter(device=self.device, status="in-progress")
            .exclude(pk=self.pk)
        )
        if qs.count() > 0:
            message = "Another upgrade operation is in progress, aborting..."
            logger.warning(message)
            self.log_line(message, save=False)
            self.status = "aborted"
            self.save()
            return
        upgrader_class = get_upgrader_class_from_device_connection(conn)
        if not upgrader_class:
            return
        upgrader = upgrader_class(self, conn)
        try:
            upgrader.upgrade(self.image.file)
        # this exception is raised when the checksum present in the device
        # equals the checksum of the image we are trying to flash, which
        # means the device was aleady flashed previously with the same image
        except UpgradeNotNeeded:
            self.status = "success"
            installed = True
        # this exception is raised when the test of the image fails,
        # meaning the image file is corrupted or not apt for flashing
        except UpgradeAborted:
            self.status = "aborted"
        # raising this exception will cause celery to retry again
        # the upgrade according to its configuration
        except RecoverableFailure as e:
            self._recoverable_failure_handler(recoverable, e)
        # failure to reconnect to the device after reflashing or any
        # other unexpected exception will flag the upgrade as failed
        except (Exception, ReconnectionFailed) as e:
            cause = str(e)
            self.log_line(cause)
            self.status = "failed"
            # if the reconnection failed we'll add some more info
            if isinstance(e, ReconnectionFailed):
                # update device connection info
                conn.is_working = False
                conn.failure_reason = cause
                conn.last_attempt = timezone.now()
                conn.save()
                # even if the reconnection failed,
                # the firmware image has been flashed
                installed = True
        # if no exception has been raised, the upgrade was successful
        else:
            installed = True
            self.status = "success"
        self.save()
        # if the firmware has been successfully installed,
        # or if it was already installed
        # set `instaleld` to `True` on the devicefirmware instance
        if installed:
            self.device.devicefirmware.installed = True
            self.device.devicefirmware.save(upgrade=False)

    def save(self, *args, **kwargs):
        result = super().save(*args, **kwargs)
        # when an operation is completed
        # trigger an update on the batch operation
        if self.batch and self.status != "in-progress":
            self.batch.update()
        return result

    @property
    def upgrader_schema(self):
        return get_upgrader_schema_for_device(self.device)

    @property
    def upgrader_class(self):
        return get_upgrader_class_for_device(self.device)
