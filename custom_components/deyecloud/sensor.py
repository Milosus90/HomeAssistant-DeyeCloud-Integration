
import logging
from datetime import timedelta, datetime
from dateutil.relativedelta import relativedelta
import hashlib
import json
import os
import aiohttp
import aiofiles
import asyncio

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    CoordinatorEntity,
    UpdateFailed,
)
# IMPORTANTE: Importar el gestor de sesiones de Home Assistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession 

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_BASE_URL,
    CONF_START_MONTH,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(minutes=1)
HISTORY_START_MONTH = "2024-01"

def _sha256(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest().lower()

async def _async_get_token(session: aiohttp.ClientSession, username, password, app_id, app_secret, base_url):
    url = f"{base_url}/account/token?appId={app_id}"
    _LOGGER.debug("Requesting token from API: %s", url)
    payload = {
        "appSecret": app_secret,
        "email": username,
        # CORRECCIÓN: Usar la contraseña original o hash según lo espere la API
        "password": password, 
        # Si la API espera el hash (como en el archivo que me enviaste):
        # "password": _sha256(password),
    }
    # Usamos la sesión pasada como argumento (la sesión gestionada por HA)
    async with session.post(url, json=payload, timeout=10) as resp:
        resp.raise_for_status()
        j = await resp.json()
        if not j.get("success"):
            _LOGGER.error("Token request failed: %s", j.get("msg"))
            raise Exception(f"Token request failed: {j.get('msg')}")
        _LOGGER.debug("Token request successful")
        return j["accessToken"]

async def _async_station_list(session, token, base_url):
    url = f"{base_url}/station/list"
    _LOGGER.debug("Fetching station list from API: %s", url)
    headers = {"Authorization": f"Bearer {token}"}
    async with session.post(url, headers=headers, json={}, timeout=10) as resp:
        resp.raise_for_status()
        stations = (await resp.json()).get("stationList", [])
        _LOGGER.info("Received %d stations from API", len(stations))
        return stations

async def _async_history(session, token, station_id, base_url):
    url = f"{base_url}/station/history"
    headers = {"Authorization": f"Bearer {token}"}
    items: list[dict] = []
    # NOTA: HISTORY_START_MONTH debe ser inicializado correctamente en async_setup_entry
    start = datetime.strptime(HISTORY_START_MONTH, "%Y-%m") 
    end = datetime.now().replace(day=1)
    _LOGGER.debug("Fetching monthly history for station_id %s from %s to %s", station_id, start.strftime("%Y-%m"), end.strftime("%Y-%m"))
    
    current = start
    while current <= end:
        range_start = current
        range_end = min(current + relativedelta(months=11), end)
        payload = {
            "stationId": station_id,
            "granularity": 3,
            "startAt": range_start.strftime("%Y-%m"),
            "endAt": range_end.strftime("%Y-%m"),
        }
        async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
            resp.raise_for_status()
            j = await resp.json()
            if not j.get("success"):
                _LOGGER.error("Monthly history request failed for station_id %s: %s", station_id, j.get("msg"))
                # Devolvemos lo que hemos conseguido hasta ahora y salimos del bucle
                return items 
            items.extend(j.get("stationDataItems", []))
        current = range_end + relativedelta(months=1)
        # Prevención de bucle infinito en caso de error en la lógica de fechas
        if current <= range_start: 
            _LOGGER.error("Date calculation error in history loop, stopping.")
            break 
            
    _LOGGER.debug("Received %d monthly records for station_id %s", len(items), station_id)
    return items

async def _async_daily_history(session, token, station_id, base_url, start_date, end_date):
    url = f"{base_url}/station/history"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "stationId": station_id,
        "granularity": 2,
        "startAt": start_date,
        "endAt": end_date,
    }
    _LOGGER.debug("Fetching daily data for station_id %s from %s to %s", station_id, start_date, end_date)
    async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
        resp.raise_for_status()
        j = await resp.json()
        if not j.get("success"):
            _LOGGER.error("Daily history request failed for station_id %s: %s", station_id, j.get("msg"))
            raise Exception(f"Daily history request failed: {j.get('msg')}")
        items = j.get("stationDataItems", [])
        _LOGGER.debug("Received %d daily records for station_id %s", len(items), station_id)
        return items

async def _async_get_device_list(session, token, base_url, stations):
    url = f"{base_url}/station/device"
    _LOGGER.debug("Fetching device list from API: %s", url)
    headers = {"Authorization": f"Bearer {token}"}
    station_ids = [st.get("id") or st.get("stationId") for st in stations if st.get("id") or st.get("stationId")]
    if not station_ids:
        _LOGGER.warning("No stationIds available for request")
        return []
    payload = {
        "page": 1,
        "size": 20,
        "stationIds": station_ids
    }
    _LOGGER.debug("Sending payload: %s", payload)
    async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
        resp.raise_for_status()
        j = await resp.json()
        if not j.get("success"):
            _LOGGER.error("Device list request failed: %s", j.get("msg"))
            raise Exception(f"Device list request failed: {j.get('msg')}")
        _LOGGER.debug("Received device list: %s", j)
        return [item["deviceSn"] for item in j.get("deviceListItems", []) if item.get("deviceType") == "INVERTER"]

async def _async_get_device_status(session, token, base_url, device_list):
    url = f"{base_url}/device/latest"
    _LOGGER.debug("Fetching device status from API: %s with devices: %s", url, device_list)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"deviceList": device_list}
    async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
        resp.raise_for_status()
        j = await resp.json()
        if not j.get("success"):
            _LOGGER.error("Device status request failed: %s", j.get("msg"))
            raise Exception(f"Device status request failed: {j.get('msg')}")
        _LOGGER.debug("Received device status: %s", j)
        return j.get("deviceDataList", [])

class DeyeCloudCoordinator(DataUpdateCoordinator):
    """Coordinator for Deye Cloud data updates."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        super().__init__(
            hass,
            _LOGGER,
            name="Deye Cloud",
            update_interval=SCAN_INTERVAL,
        )
        self.entry = entry
        self.token = None
        self.token_expiry = None
        self.data = {}  # Structure: {station_id: {info, history, daily, devices}}
        # CORRECCIÓN CLAVE: Usar la sesión HTTP gestionada por Home Assistant
        self.session = async_get_clientsession(hass) 

    async def _async_update_data(self) -> dict:
        """Fetch data from API."""
        username = self.entry.data[CONF_USERNAME]
        password = self.entry.data[CONF_PASSWORD]
        app_id = self.entry.data[CONF_APP_ID]
        app_secret = self.entry.data[CONF_APP_SECRET]
        base_url = self.entry.data[CONF_BASE_URL]
        
        # Usar la sesión gestionada por HA que está en self.session
        session = self.session 

        # Get/Refresh token
        now = datetime.now()
        if not self.token or (self.token_expiry and self.token_expiry < now):
            try:
                self.token = await _async_get_token(
                    session, username, password, app_id, app_secret, base_url
                )
                self.token_expiry = now + timedelta(minutes=30)  # Token valid for 30 mins
                _LOGGER.debug("Token refreshed, valid until %s", self.token_expiry)
            except Exception as exc:
                # Si falla la autenticación, levantamos UpdateFailed
                _LOGGER.error("Authentication failed during update: %s", exc)
                raise UpdateFailed(f"Token refresh failed: {exc}") from exc

            # Fetch stations
        try:
            stations = await _async_station_list(session, self.token, base_url)
            if not stations:
                _LOGGER.warning("No stations found for user %s", username)
                return {} # Devuelve datos vacíos si no hay estaciones
        except Exception as exc:
            # Si falla el listado de estaciones (ej. por token caducado), reintentamos autenticación en el siguiente ciclo
            self.token = None 
            _LOGGER.error("Error fetching stations, token reset: %s", exc)
            raise UpdateFailed(f"Error fetching stations: {exc}") from exc

        # Fetch data for each station concurrently
        station_data = {}
        station_tasks = []
        for station in stations:
            station_id = station.get("id") or station.get("stationId")
            if station_id:
                station_tasks.append(self._async_update_station_data(session, station_id, base_url, station))
        
        results = await asyncio.gather(*station_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                _LOGGER.error("Error updating station data: %s", result)
            elif result:
                station_id, data = result
                station_data[station_id] = data
        
        # Si no hay datos, pero el token es válido, devolvemos un error.
        if not station_data and stations:
            raise UpdateFailed("Failed to fetch data for any station.")

        return station_data

    async def _async_update_station_data(self, session, station_id, base_url, station_info):
        """Fetch data for a single station."""
        data = {"info": station_info, "history": [], "daily": {}, "devices": {}}
        
        try:
            # Fetch monthly history
            data["history"] = await _async_history(session, self.token, station_id, base_url)
            
            # Fetch daily data for yesterday and today
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            
            # Se usa el día actual y el día siguiente como endAt, según la documentación de Deye
            for date in [yesterday, today]:
                # Asumiendo que el endpoint espera el día de inicio y el día de fin
                # CORRECCIÓN: Tu archivo original tenía un error en end_date. Usaremos la misma fecha como inicio y fin.
                end_date = date 
                
                daily_data = await _async_daily_history(
                    session, self.token, station_id, base_url, date, end_date
                )
                if daily_data:
                    # La respuesta es una lista de elementos, tomamos el primero
                    data["daily"][date] = daily_data[0] 
            
            # Fetch devices
            device_sns = await _async_get_device_list(session, self.token, base_url, [station_info])
            if device_sns:
                device_status = await _async_get_device_status(session, self.token, base_url, device_sns)
                for device in device_status:
                    data["devices"][device["deviceSn"]] = device
        
        except Exception as exc:
            # Si una estación falla, solo logueamos el error y devolvemos los datos parciales.
            _LOGGER.error("Error updating data for station %s: %s", station_id, exc)
            return (station_id, data) # Devuelve datos parciales
            
        return (station_id, data)

class DeyeCloudSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Deye Cloud Sensor."""
    
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DeyeCloudCoordinator,
        sensor_type: str,
        name: str,
        unique_id: str,
        unit: str | None = None,
        device_class: str | None = None,
        state_class: str | None = None,
        extra_attributes: dict | None = None,
        station_id: str | None = None,
        date_key: str | None = None,
        metric_key: str | None = None,
        device_sn: str | None = None,
        device_key: str | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._sensor_type = sensor_type
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_native_unit_of_measurement = unit
        if device_class:
            self._attr_device_class = device_class
        if state_class:
            self._attr_state_class = state_class
        self._extra_attributes = extra_attributes or {}
        self._station_id = station_id
        self._date_key = date_key
        self._metric_key = metric_key
        self._device_sn = device_sn
        self._device_key = device_key

    @property
    def native_value(self):
        """Return the sensor value."""
        if not self.coordinator.data or not self._station_id:
            return None
            
        station_data = self.coordinator.data.get(self._station_id)
        if not station_data:
            return None

        try:
            if self._sensor_type == "monthly_raw":
                year, month = map(int, self._date_key.split("_"))
                for record in station_data.get("history", []):
                    if record["year"] == year and record["month"] == month:
                        return record.get("generationValue")

                        
            elif self._sensor_type == "monthly_metric":
                # Current or last month metric
                if self._date_key == "current":
                    now = datetime.now()
                    year, month = now.year, now.month
                else:  # last_month
                    last_month = datetime.now() - relativedelta(months=1)
                    year, month = last_month.year, last_month.month
                
                for record in station_data.get("history", []):
                    if record["year"] == year and record["month"] == month:
                        return record.get(self._metric_key)
            
            elif self._sensor_type == "daily":
                daily_data = station_data.get("daily", {}).get(self._date_key, {})
                return daily_data.get(self._metric_key)
            
            elif self._sensor_type == "device":
                device_data = station_data.get("devices", {}).get(self._device_sn, {})
                for data_item in device_data.get("dataList", []):
                    if data_item["key"] == self._device_key:
                        return data_item["value"]
        
        except (KeyError, ValueError, TypeError) as exc:
            _LOGGER.error("Error extracting value for %s: %s", self.unique_id, exc)
            
        return None

    @property
    def extra_state_attributes(self):
        """Return additional state attributes."""
        attrs = self._extra_attributes.copy()
        
        if self._station_id:
            attrs["station_id"] = self._station_id
            
        if self._date_key:
            if self._sensor_type == "monthly_raw":
                attrs["year"] = int(self._date_key.split("_")[0])
                attrs["month"] = int(self._date_key.split("_")[1])
            elif self._sensor_type == "daily":
                attrs["date"] = self._date_key
                
        if self._device_sn:
            attrs["device_sn"] = self._device_sn
            
        return attrs

    @property
    def device_info(self):
        """Return the device information."""
        if not self.coordinator.data or not self._station_id:
            return None
            
        station_info = self.coordinator.data.get(self._station_id, {}).get("info", {})
        
        # Definimos el nombre de la estación como "Deye Solar" para evitar el nombre del propietario
        # station_name = station_info.get("name", f"Station {self._station_id}") # Originalmente usaba el nombre de la API
        station_name = "Deye Solar"
        
        # --- CORRECCIÓN de Tipo de Datos (TypeError fix) ---
        station_identifier_tuple = (DOMAIN, str(self._station_id)) 
        station_identifiers_set = {station_identifier_tuple} 
        # ----------------------------------------------------
        
        # Device specific info
        if self._sensor_type == "device":
            device_sn = self._device_sn
            device_data = self.coordinator.data.get(self._station_id, {}).get("devices", {}).get(device_sn, {})
            name = f"{device_data.get('deviceType', 'Device')} ({device_sn})"
            model = device_data.get('deviceType', 'Unknown Device')
            identifiers = {(DOMAIN, device_sn)}
            # Uso de la tupla para via_device
            via_device = station_identifier_tuple
            manufacturer = "Deye"
            
            return {
                "identifiers": identifiers,
                "name": name,
                "manufacturer": manufacturer,
                "model": model,
                "via_device": via_device,
            }
            
        # Station Info
        return {
            # Uso del set de identificadores para el dispositivo principal
            "identifiers": station_identifiers_set, 
            "name": station_name,
            "manufacturer": "Deye",
            "model": "Solar Station",
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    _LOGGER.info("Setting up DeyeCloud integration")
    global HISTORY_START_MONTH
    HISTORY_START_MONTH = entry.data.get(CONF_START_MONTH, "2024-01")
    _LOGGER.debug("HISTORY_START_MONTH set to: %s", HISTORY_START_MONTH)

    coordinator = DeyeCloudCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entities = []
    now = datetime.now()
    this_year, this_month = now.year, now.month
    last_month_dt = now - relativedelta(months=1)
    prev_year, prev_month = last_month_dt.year, last_month_dt.month
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    relative_days = {
        today: " (Hoy)",
        yesterday: " (Ayer)",
    }

    _MONTHLY_METRICS = [
        ("generationValue", "Solar Generation"),
        ("consumptionValue", "Monthly Consumption"),
        ("gridValue", "Monthly Grid Export"),
        ("purchaseValue", "Monthly Grid Import"),
        ("chargeValue", "Monthly Battery Charge"),
        ("dischargeValue", "Monthly Battery Discharge"),
    ]

    _DAILY_METRICS = [
        ("generationValue", "Solar Generation"),
        ("consumptionValue", "Daily Consumption"),
        ("gridValue", "Daily Grid Export"),
        ("purchaseValue", "Daily Grid Import"),
        ("chargeValue", "Daily Battery Charge"),
        ("dischargeValue", "Daily Battery Discharge"),
    ]

    for station_id, station_data in coordinator.data.items():
        # --- CORRECCIÓN para Nombre del Sensor (Línea 420-421) ---
        # Usa un nombre genérico para evitar el nombre del propietario.
        station_name = "Deye Solar" 
        # -------------------------------------------------------------
        
        # ==== MONTHLY RAW ====
        for record in station_data.get("history", []):
            y, m = record['year'], record['month']
            month_name = datetime(year=y, month=m, day=1).strftime("%b %Y")
            name = f"{station_name} {month_name}"
            uid = f"{station_id}_raw_{y}_{m:02d}"
            entities.append(DeyeCloudSensor(
                coordinator=coordinator,
                sensor_type="monthly_raw",
                name=name,
                unique_id=uid,
                unit="kWh",
                device_class="energy",
                state_class="total_increasing",
                station_id=station_id,
                date_key=f"{y}_{m}",
                extra_attributes=record,
            ))

        # ==== MONTHLY METRICS ====
        for metric_key, metric_name in _MONTHLY_METRICS:
            # Current month
            name = f"{station_name} {metric_name} (Mes actual)"
            uid = f"{station_id}_{metric_key}_current_month"
            entities.append(DeyeCloudSensor(
                coordinator=coordinator,
                sensor_type="monthly_metric",
                name=name,
                unique_id=uid,
                unit="kWh",
                device_class="energy",
                state_class="total_increasing",
                station_id=station_id,
                date_key="current",
                metric_key=metric_key,
                extra_attributes={
                    "year": this_year,
                    "month": this_month,
                    "metric": metric_name,
                }
            ))

            # Last month
            name = f"{station_name} {metric_name} (Mes anterior)"
            uid = f"{station_id}_{metric_key}_last_month"
            entities.append(DeyeCloudSensor(
                coordinator=coordinator,
                sensor_type="monthly_metric",
                name=name,
                unique_id=uid,
                unit="kWh",
                device_class="energy",
                state_class="total_increasing",
                station_id=station_id,
                date_key="last",
                metric_key=metric_key,
                extra_attributes={
                    "year": prev_year,
                    "month": prev_month,
                    "metric": metric_name,
                }
            ))

        # ==== DAILY ====
        for date in [yesterday, today]:
            rel_suffix = relative_days.get(date, f" ({date})")
            for metric_key, metric_name in _DAILY_METRICS:
                name = f"{station_name} {metric_name}{rel_suffix}"
                uid = f"{station_id}_{metric_key}{date.replace('-', '_')}"
                if station_data["daily"].get(date): # Solo crear sensor si hay datos
                    entities.append(DeyeCloudSensor(
                        coordinator=coordinator,
                        sensor_type="daily",
                        name=name,
                        unique_id=uid,
                        unit="kWh",
                        device_class="energy",
                        state_class="total_increasing",
                        station_id=station_id,
                        date_key=date,
                        metric_key=metric_key,
                        extra_attributes={"date": date}
                    ))

        # ==== DEVICE STATUS ====
        for device_sn, device_data in station_data.get("devices", {}).items():
            device_type = device_data.get("deviceType", "Dispositivo")
            for data_item in device_data.get("dataList", []):
                key = data_item["key"]
                name = f"{station_name} {device_type} {key}"
                uid = f"device_{device_sn}_{key}"
                entities.append(DeyeCloudSensor(
                    coordinator=coordinator,
                    sensor_type="device",
                    name=name,
                    unique_id=uid,
                    unit=data_item.get("unit", ""),
                    device_class=None,
                    state_class=None,
                    station_id=station_id,
                    device_sn=device_sn,
                    device_key=key,
                    extra_attributes={
                        "device_type": device_data.get("deviceType"),
                        "device_state": device_data.get("deviceState"),
                        "collection_time": device_data.get("collectionTime"),
                    }
                ))

    async_add_entities(entities)
    _LOGGER.info("DeyeCloud integration setup completed with %d sensors", len(entities))
    return True