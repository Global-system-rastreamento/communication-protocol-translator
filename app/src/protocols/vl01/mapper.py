import struct
from datetime import datetime, timezone, timedelta
import json
import copy

from app.core.logger import get_logger
from app.services.redis_service import get_redis
from app.src.suntech.utils import build_suntech_packet, build_suntech_alv_packet, build_suntech_res_packet
from app.src.connection.main_server_connection import send_to_main_server

logger = get_logger(__name__)
redis_client = get_redis()


VL01_TO_SUNTECH_ALERT_MAP = {
    0x01: 42,  # SOS -> Suntech: Panic Button
    0x02: 41,  # Power Cut Alarm -> Suntech: Backup Battery Disconnected
    0x03: 15,  # Shock Alarm -> Suntech: Shocked
    0x04: 6,   # Fence In Alarm -> Suntech: Enter Geo-Fence
    0x05: 5,   # Fence Out Alarm -> Suntech: Exit Geo-Fence
    0x06: 1,   # Overspeed Alarm -> Suntech: Over Speed
    0x19: 14,  # Battery low voltage alarm -> Suntech: Battery Low
    0xF0: 46,  # Urgent acceleration alarm -> Suntech: Harsh Acceleration
    0xF1: 47,  # Rapid deceleration alarm -> Suntech: Harsh Braking
    0x13: 147, # Remove alarm -> Suntech: Absent Device Recovered
    0x14: 73,  # car door alarm -> Suntech: Anti-theft
    0xFE: 33,  # ACC On -> Suntech: Ignition On
    0xFF: 34   # ACC Off -> Suntech: Ignition Off
}


def decode_location_packet(body: bytes):

    try:
        data = {}

        year, month, day, hour, minute, second = struct.unpack(">BBBBBB", body[0:6])
        data["timestamp"] = datetime(2000 + year, month, day, hour, minute, second).replace(tzinfo=timezone.utc)

        sats_byte = body[6]
        data["satellites"] = sats_byte & 0x0F

        lat_raw, lon_raw = struct.unpack(">II", body[7:15])
        lat = lat_raw / 1800000.0
        lon = lon_raw / 1800000.0

        data["speed_kmh"] = body[15]

        course_status = struct.unpack(">H", body[16:18])[0]

        # Hemisférios (Bit 11 para Latitude Sul, Bit 12 para Longitude Oeste)
        is_latitude_north = (course_status >> 10) & 1
        is_longitude_west = (course_status >> 11) & 1
        
        data['latitude'] = -abs(lat) if not is_latitude_north else abs(lat)
        data['longitude'] = -abs(lon) if is_longitude_west else abs(lon)
            
        data["direction"] = course_status & 0x03FF

        gps_fixed = (course_status >> 12) & 1

        try: # Colocando dentro de um try except pois essa funcao tbm recebe chamadas para decodificar pacotes de alerta, que não tem as infos abaixo, 
            # Na ordem em que estão abaixo
            mcc = struct.unpack(">H", body[18:20])[0]
            mnc_length = 1
            if (mcc >> 15) & 1:
                mnc_length = 2

            acc_at = 20 + mnc_length + 4 + 8
            acc_status = body[acc_at]
            status_bits = 0
            if gps_fixed == 1:
                status_bits |= 0b10
            if acc_status == 1:
                status_bits |= 0b1
            data["status_bits"] = status_bits

            is_realtime = body[acc_at + 2] == 0x00

            data["is_realtime"] = is_realtime

            mileage_at = acc_at + 3
            mileage_km = struct.unpack(">I", body[mileage_at:mileage_at + 4])[0]
            data["gps_odometer"] = mileage_km
        except:
            pass

        return data

    except Exception as e:
        logger.exception(f"Falha ao decodificar pacote de localização VL01 body_hex={body.hex()}")
        return None


def handle_location_packet(dev_id_str: str, serial: int, body: bytes):
    location_data = decode_location_packet(body)

    if not location_data:
        return
    
    last_location_data = copy.deepcopy(location_data)
    
    last_location_data["timestamp"] = last_location_data["timestamp"].strftime("%Y-%m-%dT%H:%M:%S")

    # Salvando para uso em caso de alarmes
    redis_client.hset(dev_id_str, "last_location_data", json.dumps(last_location_data))

    suntech_packet = build_suntech_packet(
        "STT",
        dev_id_str,
        location_data,
        serial,
        location_data.get("is_realtime", True)
    )

    if suntech_packet:
        logger.info(f"Pacote Localização SUNTECH traduzido de pacote VL01:\n{suntech_packet}")
        send_to_main_server(dev_id_str, serial, suntech_packet.encode("ascii"))

def handle_alarm_packet(dev_id_str: str, serial: int, body: bytes):

    if len(body) < 21:
        logger.info(f"Pacote de dados de alarme recebido com um tamanho menor do que o esperado, body={body.hex()}")
        return
    
    alarm_location_data = decode_location_packet(body[0:16])

    alarm_datetime = alarm_location_data.get("timestamp")
    if not alarm_datetime:
        logger.info(f"Pacote de alarme sem data e hora, descartando... dev_id={dev_id_str}")
        return
    
    limit = datetime.now(timezone.utc) - timedelta(minutes=2)

    if not alarm_datetime > limit:
        logger.info(f"Alarme da memória, descartando... dev_id={dev_id_str}")

    last_location_data_str = redis_client.hget(dev_id_str, "last_location_data")
    last_location_data = json.loads(last_location_data_str)

    definitive_location_data = {**last_location_data, **alarm_location_data}

    if not definitive_location_data:
        return
    
    alarm_code = body[17]

    suntech_alert_id = VL01_TO_SUNTECH_ALERT_MAP.get(alarm_code)

    if suntech_alert_id:
        logger.info(f"Alarme VL01 (0x{alarm_code:02X}) traduzido para Suntech ID {suntech_alert_id} device_id={dev_id_str}")
        suntech_packet = build_suntech_packet(
            hdr="ALT",
            dev_id=dev_id_str,
            location_data=definitive_location_data,
            serial=serial,
            is_realtime=True,
            alert_id=suntech_alert_id
        )
        if suntech_packet:
            logger.info(f"Pacote Alerta SUNTECH traduzido de pacote VL01:\n{suntech_packet}")

            send_to_main_server(dev_id_str, serial, suntech_packet.encode('ascii'))
    else:
        logger.warning(f"Alarme VL01 não mapeado recebido device_id={dev_id_str}, alarm_code={hex(alarm_code)}")

def handle_heartbeat_packet(dev_id_str: str, serial: int, body: bytes):
    # O pacote de Heartbeat (0x13) contém informações de status
    terminal_info = body[0]

    output_status = (terminal_info >> 7) & 0b1
    redis_client.hset(dev_id_str, "last_output_status", output_status)

    # Keep-Alive da Suntech
    suntech_packet = build_suntech_alv_packet(dev_id_str)
    if suntech_packet:
        logger.info(f"Pacote de Heartbeat/KeepAlive SUNTECH traduzido de pacote VL01:\n{suntech_packet}")
        send_to_main_server(dev_id_str, serial, suntech_packet.encode('ascii'))

def handle_reply_command_packet(dev_id: str, serial: int, body: bytes):
    try:
        command_content = body[5:-4]
        command_content_str = command_content.decode("ascii", errors="ignore")

        if command_content_str:
            last_location_data_str = redis_client.hget(dev_id, "last_location_data")
            last_location_data = json.loads(last_location_data_str)
            last_location_data["timestamp"] = datetime.now(timezone.utc)

            packet = None
            
            if command_content_str == "RELAY 1":
                packet = build_suntech_res_packet(dev_id, ["CMD", dev_id, "04", "01"], last_location_data)
            elif command_content_str == "RELAY 0":
                packet = build_suntech_res_packet(dev_id, ["CMD", dev_id, "04", "02"], last_location_data)
            else:
                print(command_content_str)
                
            if packet:
                send_to_main_server(dev_id, serial, packet.encode("ascii"))

            pass
    except Exception as e:
        logger.error(f"Erro ao decodificar comando de REPLY")