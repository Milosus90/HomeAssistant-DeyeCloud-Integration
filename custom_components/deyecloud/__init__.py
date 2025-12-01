# __init__.py
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

async def async_setup(hass: HomeAssistant, config: dict):
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])  
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    return await hass.config_entries.async_forward_entry_unload(entry, "sensor")

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the DeyeCloud config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)