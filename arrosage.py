#!/usr/bin/env python3
"""Décision automatique d'arrosage selon la météo (OpenWeatherMap).

Codes de sortie :
    0 -> arrosage autorisé
    1 -> arrosage annulé (conditions météo défavorables)
    2 -> arrosage annulé (erreur : mode sécurisé)
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "arrosage.log"

URL_ACTUELLE = "https://api.openweathermap.org/data/2.5/weather"
URL_PREVISIONS = "https://api.openweathermap.org/data/2.5/forecast"

TIMEOUT_S = 10          # délai maximal par requête HTTP
NB_CRENEAUX_24H = 8     # l'API de prévisions fournit un créneau par 3 h -> 8 x 3 h = 24 h

CLES_REQUISES = (
    "api_key",
    "lat",
    "lon",
    "seuil_pluie_mm_24h",
    "seuil_temperature_min",
    "mode_dry_run",
)

logger = logging.getLogger("arrosage")


# --------------------------------------------------------------------------- #
# Modèles de données
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    api_key: str
    lat: float
    lon: float
    seuil_pluie_mm_24h: float
    seuil_temperature_min: float
    mode_dry_run: bool


@dataclass(frozen=True)
class Meteo:
    temperature_c: float
    precip_recente_mm: float   # pluie + neige (équivalent en eau) de la dernière heure
    precip_prevue_mm: float    # pluie + neige prévues sur les 24 prochaines heures

    @property
    def cumul_24h_mm(self) -> float:
        return self.precip_recente_mm + self.precip_prevue_mm


@dataclass(frozen=True)
class Decision:
    arroser: bool
    raisons: list[str]


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def configurer_logging() -> None:
    """Journalise à la fois dans la console et dans arrosage.log."""
    logger.setLevel(logging.INFO)
    formatteur = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatteur)

    fichier = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fichier.setFormatter(formatteur)

    logger.addHandler(console)
    logger.addHandler(fichier)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def charger_config(chemin: Path = CONFIG_PATH) -> Config:
    """Lit et valide config.json. Lève ValueError/OSError en cas de problème."""
    with chemin.open(encoding="utf-8") as f:
        brut: dict[str, Any] = json.load(f)

    manquantes = [cle for cle in CLES_REQUISES if cle not in brut]
    if manquantes:
        raise ValueError(f"Clés manquantes dans {chemin.name} : {', '.join(manquantes)}")

    api_key = str(brut["api_key"]).strip()
    if not api_key or api_key == "YOUR_OPENWEATHERMAP_API_KEY":
        raise ValueError("La clé API OpenWeatherMap n’est pas renseignée dans config.json.")

    if not isinstance(brut["mode_dry_run"], bool):
        raise ValueError("« mode_dry_run » doit être un booléen (true / false).")

    return Config(
        api_key=api_key,
        lat=float(brut["lat"]),
        lon=float(brut["lon"]),
        seuil_pluie_mm_24h=float(brut["seuil_pluie_mm_24h"]),
        seuil_temperature_min=float(brut["seuil_temperature_min"]),
        mode_dry_run=brut["mode_dry_run"],
    )


# --------------------------------------------------------------------------- #
# Appels API
# --------------------------------------------------------------------------- #
def _appeler_api(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET JSON avec délai d'attente ; lève requests.RequestException en cas d'échec."""
    reponse = requests.get(url, params=params, timeout=TIMEOUT_S)
    reponse.raise_for_status()
    return reponse.json()


def _precip_mm(bloc: dict[str, Any], cle_duree: str) -> float:
    """Additionne pluie + neige (mm d'eau) d'un bloc OpenWeatherMap."""
    pluie = float(bloc.get("rain", {}).get(cle_duree, 0.0))
    neige = float(bloc.get("snow", {}).get(cle_duree, 0.0))
    return pluie + neige


def recuperer_meteo(config: Config) -> Meteo:
    """Interroge les endpoints Current et Forecast d'OpenWeatherMap."""
    params = {
        "lat": config.lat,
        "lon": config.lon,
        "appid": config.api_key,
        "units": "metric",
    }

    # 1) Conditions actuelles : température + précipitations de la dernière heure
    actuel = _appeler_api(URL_ACTUELLE, params)
    temperature = float(actuel["main"]["temp"])
    recente = _precip_mm(actuel, "1h")

    # 2) Prévisions : 8 créneaux de 3 h = 24 h
    previsions = _appeler_api(URL_PREVISIONS, {**params, "cnt": NB_CRENEAUX_24H})
    creneaux = previsions["list"][:NB_CRENEAUX_24H]
    if not creneaux:
        raise ValueError("Réponse de prévisions vide.")
    prevue = sum(_precip_mm(c, "3h") for c in creneaux)

    return Meteo(temperature_c=temperature, precip_recente_mm=recente, precip_prevue_mm=prevue)


# --------------------------------------------------------------------------- #
# Logique de décision
# --------------------------------------------------------------------------- #
def decider(meteo: Meteo, config: Config) -> Decision:
    """Autorise l'arrosage uniquement si toutes les conditions sont favorables."""
    raisons: list[str] = []

    if meteo.temperature_c < config.seuil_temperature_min:
        raisons.append(
            f"Température trop basse : {meteo.temperature_c:.1f} °C "
            f"< seuil de {config.seuil_temperature_min:.1f} °C"
        )

    if meteo.cumul_24h_mm >= config.seuil_pluie_mm_24h:
        raisons.append(
            f"Précipitations suffisantes : {meteo.cumul_24h_mm:.1f} mm "
            f"(récent {meteo.precip_recente_mm:.1f} + prévu {meteo.precip_prevue_mm:.1f}) "
            f">= seuil de {config.seuil_pluie_mm_24h:.1f} mm"
        )

    if raisons:
        return Decision(arroser=False, raisons=raisons)

    return Decision(
        arroser=True,
        raisons=[
            f"Conditions favorables : {meteo.temperature_c:.1f} °C "
            f"(>= {config.seuil_temperature_min:.1f} °C) et {meteo.cumul_24h_mm:.1f} mm "
            f"de précipitations (< {config.seuil_pluie_mm_24h:.1f} mm)"
        ],
    )


# --------------------------------------------------------------------------- #
# Action matérielle
# --------------------------------------------------------------------------- #
def declencher_arrosage() -> None:
    """Point d'intégration matériel : À ADAPTER à votre installation.

    Exemples : relais via GPIO (gpiozero), requête HTTP vers un contrôleur
    (Home Assistant, Shelly, OpenSprinkler), etc.
    """
    logger.info("Signal d’arrosage envoyé au matériel (à implémenter dans declencher_arrosage()).")


# --------------------------------------------------------------------------- #
# Programme principal
# --------------------------------------------------------------------------- #
def main() -> int:
    configurer_logging()
    logger.info("=== Évaluation de l’arrosage ===")

    # Configuration
    try:
        config = charger_config()
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.error("Configuration invalide : %s -> PAS D’ARROSAGE (mode sécurisé).", exc)
        return 2

    if config.mode_dry_run:
        logger.info("Mode DRY-RUN activé : aucun signal matériel ne sera envoyé.")

    # Météo
    try:
        meteo = recuperer_meteo(config)
    except requests.HTTPError as exc:
        statut = exc.response.status_code if exc.response is not None else "?"
        indice = " (clé API invalide ou pas encore activée ?)" if statut == 401 else ""
        logger.error("Erreur HTTP %s de l’API météo%s -> PAS D’ARROSAGE (mode sécurisé).", statut, indice)
        return 2
    except requests.RequestException as exc:
        logger.error("Panne réseau/API : %s -> PAS D’ARROSAGE (mode sécurisé).", exc)
        return 2
    except (KeyError, ValueError, TypeError) as exc:
        logger.error("Réponse API inattendue (%r) -> PAS D’ARROSAGE (mode sécurisé).", exc)
        return 2

    logger.info(
        "Météo : %.1f °C | précip. récentes (1 h) : %.1f mm | précip. prévues (24 h) : %.1f mm",
        meteo.temperature_c,
        meteo.precip_recente_mm,
        meteo.precip_prevue_mm,
    )

    # Décision
    decision = decider(meteo, config)
    libelle = "ARROSAGE AUTORISÉ" if decision.arroser else "ARROSAGE ANNULÉ"
    logger.info("DÉCISION : %s", libelle)
    for raison in decision.raisons:
        logger.info("  - %s", raison)

    # Action
    if decision.arroser:
        if config.mode_dry_run:
            logger.info("[DRY-RUN] Décision consignée uniquement, aucun signal matériel émis.")
        else:
            declencher_arrosage()
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
