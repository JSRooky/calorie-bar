"""Listen to a Tacx Vortex Smart over ANT+.

The trainer broadcasts two device types at once:

* Bicycle Power (type 11), page 0x10: instantaneous power and cadence.
* Fitness Equipment / FE-C (type 17):
  page 0x19 power and cadence, page 0x10 speed and state,
  page 0x11 resistance and grade.

Rouvy can keep the Bluetooth connection. This only receives the ANT+ broadcast.
"""

from openant.devices import ANTPLUS_NETWORK_KEY
from openant.devices.fitness_equipment import FitnessEquipment, FitnessEquipmentData
from openant.devices.power_meter import PowerData, PowerMeter
from openant.easy.node import Node


def _cadence(rpm: int) -> str:
    # 255 is the ANT+ "invalid" sentinel.
    return "—" if rpm == 255 else f"{rpm} rpm"


def main() -> None:
    node = Node()
    node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)

    # device_id=0 pairs with the first device of that type.
    power = PowerMeter(node, device_id=0)
    trainer = FitnessEquipment(node, device_id=0)

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
