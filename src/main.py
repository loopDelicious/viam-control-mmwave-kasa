import asyncio
from threading import Event
from typing import ClassVar, Mapping, Optional, Sequence, cast
from typing_extensions import Self
from viam.logging import getLogger
from viam.module.module import Module
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.easy_resource import EasyResource
from viam.resource.types import Model, ModelFamily
from viam.utils import struct_to_dict, ValueTypes
from viam.components.sensor import Sensor
from viam.services.generic import Generic as GenericServiceBase
from viam.components.generic import Generic as GenericComponent

LOGGER = getLogger("mmwave-kasa")

class MmwaveKasa(GenericServiceBase, EasyResource):
    MODEL: ClassVar[Model] = Model(
        ModelFamily("joyce", "mmwave-kasa"), "mmwave-kasa"
    )

    auto_start = True
    task = None
    event = Event()
    light_on = False
    turn_off_task = None  # Task to handle delayed turn off

    @classmethod
    def new(
        cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ) -> Self:
        """Factory method to create a new instance."""
        return super().new(config, dependencies)

    @classmethod
    def validate_config(cls, config: ComponentConfig) -> Sequence[str]:
        fields = struct_to_dict(config.attributes)
        required_keys = ["sensor", "kasa"]

        for key in required_keys:
            if key not in fields or not isinstance(fields[key], str):
                raise ValueError(f"{key} must be a string and included in the configuration.")

        return [fields["sensor"], fields["kasa"]]

    def reconfigure(
        self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]
    ):
        """Update resource configuration dynamically."""
        attrs = struct_to_dict(config.attributes)
        self.auto_start = bool(attrs.get("auto_start", self.auto_start))

        # Fetch mmWave sensor
        sensor_resource = dependencies.get(
            Sensor.get_resource_name(str(attrs.get("sensor")))
        )
        self.sensor = cast(Sensor, sensor_resource)

        # Fetch Kasa smart plug
        kasa_resource = dependencies.get(
            GenericComponent.get_resource_name(str(attrs.get("kasa")))
        )
        self.kasa = cast(GenericComponent, kasa_resource)

        if self.auto_start:
            self.start()

        return super().reconfigure(config, dependencies)

    async def on_loop(self):
        """Continuously fetch sensor readings and toggle the Kasa smart plug."""
        LOGGER.info("Process started - Checking sensor readings")

        while not self.event.is_set():
            try:
                readings = await self.sensor.get_readings() if self.sensor else {}
                LOGGER.info(f"Sensor Readings: {readings}")

                # Parse detection status
                detection_status = readings.get("detection_status", "No Target")
                presence_detected = detection_status in ["Moving Target", "Moving and Static Targets", "Static Target"]

                if presence_detected:
                    if not self.light_on:
                        LOGGER.info("Presence detected! Turning Kasa Plug ON.")
                        try:
                            response = await self.kasa.do_command({"toggle_on": []})
                            LOGGER.info(f"Kasa toggle_on command sent. Response: {response}")
                            self.light_on = True  # Mark light as ON
                        except Exception as e:
                            LOGGER.error(f"Failed to turn on Kasa plug: {e}")

                    # Reset any pending turn-off task
                    if self.turn_off_task and not self.turn_off_task.done():
                        LOGGER.info("Presence detected again - canceling turn-off task.")
                        self.turn_off_task.cancel()

                else:
                    if self.light_on and (not self.turn_off_task or self.turn_off_task.done()):
                        LOGGER.info("No presence detected. Scheduling turn off in 10 seconds.")
                        self.turn_off_task = asyncio.create_task(self.delayed_turn_off())

            except Exception as e:
                LOGGER.error(f"Error updating Kasa plug: {e}")

            await asyncio.sleep(1)  # Check every second

    async def delayed_turn_off(self):
        """Delays toggling off the Kasa plug if no presence is detected for 10 seconds."""
        try:
            await asyncio.sleep(10)  # Wait 10 seconds before turning off
            LOGGER.info("Toggling Kasa Plug OFF after 10 seconds of no presence.")
            await self.kasa.do_command({"toggle_off": []})
            self.light_on = False  # Mark light as OFF
        except asyncio.CancelledError:
            LOGGER.info("Toggle-off task was canceled because presence was detected again.")

    def start(self):
        """Start background loop only if not already running."""
        if self.task is None or self.task.done():
            self.event.clear()  # Ensures loop runs
            self.task = asyncio.create_task(self.control_loop())

    def stop(self):
        """Stop the service loop gracefully."""
        self.event.set()
        if self.task is not None:
            self.task.cancel()
        if self.turn_off_task is not None:
            self.turn_off_task.cancel()

    async def control_loop(self):
        """Persistent control loop handling Kasa plug updates."""
        while not self.event.is_set():
            await self.on_loop()
            await asyncio.sleep(0)

    def __del__(self):
        self.stop()

    async def close(self):
        self.stop()


if __name__ == "__main__":
    asyncio.run(Module.run_from_registry())
