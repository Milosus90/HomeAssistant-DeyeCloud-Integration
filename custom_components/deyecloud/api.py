# api.py
import hashlib
import aiohttp
import logging

_LOGGER = logging.getLogger(__name__)

def _sha256(password: str) -> str:
    """Función auxiliar para hashear la contraseña."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest().lower()

async def async_get_token(session: aiohttp.ClientSession, username, password, app_id, app_secret, base_url):
    """Obtiene el token de autenticación de la API de DeyeCloud."""
    url = f"{base_url}/account/token?appId={app_id}"
    _LOGGER.debug("Requesting token from API: %s", url)
    payload = {
        "appSecret": app_secret,
        "email": username,
        "password": _sha256(password),
    }
    
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            # 1. Verifica errores HTTP (4xx, 5xx)
            if resp.status >= 400:
                error_text = await resp.text()
                _LOGGER.error("HTTP Error %s during token request. Full response: %s", resp.status, error_text)
                resp.raise_for_status()

            j = await resp.json()
            
            # 2. Verifica si la API de Deye indica que falló
            if not j.get("success"):
                _LOGGER.error("Deye API reported failure (success=false) for token request. Response JSON: %s", j)
                msg = j.get('msg') or "Credenciales incorrectas (API o Usuario). La API no proporcionó mensaje."
                raise Exception(f"Token request failed: {msg}")
            
            return j["accessToken"]

    except aiohttp.client_exceptions.ClientConnectorError as e:
        # Esto captura el 'Connection closed' que estás viendo.
        _LOGGER.error("Error de conexión durante la autenticación: %s", e)
        raise Exception("Fallo de conexión (Connection closed). Verifica la Base URL y la red, o revisa si el App ID/Secret está bloqueando la conexión.")
    except Exception as e:
        # Captura errores HTTP y otros fallos genéricos
        raise e