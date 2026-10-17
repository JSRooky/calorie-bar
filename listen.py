"""Listen to a Tacx Vortex Smart over ANT+.

The trainer broadcasts two device types at once:

* Bicycle Power (type 11), page 0x10: instantaneous power and cadence.
* Fitness Equipment / FE-C (type 17):
  page 0x19 power and cadence, page 0x10 speed and state,
  page 0x11 resistance and grade.

Rouvy can keep the Bluetooth connection. This only receives the ANT+ broadcast.
"""

import logging
import threading

# libusb0 on Windows cannot query or reattach a kernel driver. openant logs that as a warning.
logging.getLogger("openant.base.driver").setLevel(logging.ERROR)


class _IgnoreLibusbTimeout(logging.Filter):
    """libusb0 reports an idle read as USBError instead of a timeout."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "_usb_reap_async" not in record.getMessage()


logging.getLogger("openant.base.ant").addFilter(_IgnoreLibusbTimeout())

from openant.devices import ANTPLUS_NETWORK_KEY
from openant.devices.fitness_equipment import FitnessEquipment, FitnessEquipmentData
from openant.devices.power_meter import PowerData, PowerMeter
from openant.easy.node import Node


def _keep_first_device(device) -> None:
    """Remember the first device id without reopening the channel.

    openant closes and reopens a wildcard channel on the first packet.
    This stick answers that reopen with CHANNEL_IN_WRONG_STATE and the
    listener dies, so the bar never stays connected.
    """
    original = device._on_data

    def on_data(data):
        if len(data) > 8 and device.device_id == 0:
            device.device_id = data[9] + (data[10] << 8)
            device.trans_type = data[12]
        original(data)

    device._on_data = on_data
    device.channel.on_broadcast_data = on_data
    device.channel.on_burst_data = on_data
    device.channel.on_acknowledge = on_data


def _cadence(rpm: int) -> str:
    # 255 is the ANT+ "invalid" sentinel.
    return "—" if rpm == 255 else f"{rpm} rpm"


class AntSession:
    """Search for a power meter and a trainer, then report watts.

    The power-meter channel wins when both broadcast the same trainer,
    so the reading is not counted twice.
    """

    def __init__(self) -> None:
        self._node: Node | None = None
        self._thread: threading.Thread | None = None

    def start(self, on_link, on_watts) -> None:
        self._thread = threading.Thread(
            target=self._run,
            args=(on_link, on_watts),
            name="ant-session",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        node = self._node
        if node is not None:
            try:
                node.stop()
            except Exception:
                pass

    def _run(self, on_link, on_watts) -> None:
        node = None
        power = None
        trainer = None
        meter_seen = False
        try:
            node = Node()
            self._node = node
            node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
            power = PowerMeter(node, device_id=0)
            trainer = FitnessEquipment(node, device_id=0)
            _keep_first_device(power)
            _keep_first_device(trainer)

            def found() -> None:
                on_link("on")

            def on_power(_page: int, page_name: str, data: PowerData) -> None:
                nonlocal meter_seen
                if page_name != "standard_power":
                    return
                meter_seen = True
                on_watts(float(data.instantaneous_power))

            def on_trainer(page: int, _page_name: str, data) -> None:
                if meter_seen or not isinstance(data, PowerData) or page != 0x19:
                    return
                on_watts(float(data.instantaneous_power))

            power.on_found = found
            trainer.on_found = found
            power.on_device_data = on_power
            trainer.on_device_data = on_trainer
            node.start()
        except Exception:
            on_link("off")
        finally:
            if node is not None:
                try:
                    node.stop()
                except Exception:
                    pass


def main() -> None:
    node = Node()
    node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)

    # device_id=0 pairs with the first device of that type.
    power = PowerMeter(node, device_id=0)
    trainer = FitnessEquipment(node, device_id=0)
    _keep_first_device(power)
    _keep_first_device(trainer)

    def on_power_found() -> None:
        print(f"power meter found: {power}")

    def on_trainer_found() -> None:
        print(f"trainer found: {trainer}")

    def on_power(_page: int, page_name: str, data: PowerData) -> None:
        if page_name != "standard_power":
            return
        print(
            f"power  {data.instantaneous_power:4d} W"
            f"   avg {data.average_power:4d} W"
            f"   cadence {_cadence(data.cadence)}"
        )

    def on_trainer(page: int, page_name: str, data) -> None:
        if isinstance(data, PowerData) and page == 0x19:
            print(
                f"fe-c   {data.instantaneous_power:4d} W"
                f"   avg {data.average_power:4d} W"
                f"   cadence {_cadence(data.cadence)}"
            )
            return
        if not isinstance(data, FitnessEquipmentData):
            return
        if page_name == "general_fe":
            speed_kmh = data.speed * 3.6
            print(f"speed  {speed_kmh:5.1f} km/h   state {data.state.name}")
        elif page_name == "general_settings":
            grade = "—" if data.incline == 32767.0 else f"{data.incline:.1f} %"
            print(f"grade  {grade}   resistance {data.resistance:.1f} %")

    power.on_found = on_power_found
    power.on_device_data = on_power
    trainer.on_found = on_trainer_found
    trainer.on_device_data = on_trainer

    print("Listening for Vortex. Pedal so it broadcasts. Ctrl-C to stop.")
    try:
        node.start()
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        power.close_channel()
        trainer.close_channel()
        node.stop()


if __name__ == "__main__":
    main()
