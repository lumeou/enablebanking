"""Category rules: load categories.yaml, match a transaction against it.

Manual corrections (category_source='manual' in the DB) are never overwritten
by rule-based (re)categorization.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

_LOGGER = logging.getLogger(__name__)

DEFAULT_RULES = """\
# Reglas de categorización para Enable Banking.
# Cada regla: category, y al menos uno de contains / regex / mcc.
# - contains: coincide si el texto (contraparte + concepto) contiene cualquiera de estas palabras (sin mayúsculas/minúsculas).
# - regex: expresión regular sobre el mismo texto.
# - mcc: lista de códigos de categoría de comercio (merchant_category_code).
# Las reglas se prueban en orden; gana la primera que coincida.
rules:
  - category: Alimentación
    contains: ["mercadona", "lidl", "carrefour", "dia", "aldi", "eroski", "froiz", "hipercor", "supermercado", "supermercados"]
  - category: Restauración
    contains: ["restaurante", "cafeteria", "cafetería", "bar ", "meson", "cafe-bar", "heladeria", "heladería", "pasteleria", "pastelería"]
  - category: Transporte
    contains: ["renfe", "metro", "emt", "uber", "cabify", "gasolinera", "repsol", "cepsa", "parking", "aparcamiento"]
  - category: Vivienda
    contains: ["alquiler", "hipoteca", "comunidad de propietarios"]
  - category: Suministros
    contains: ["endesa", "iberdrola", "naturgy", "movistar", "vodafone", "orange", "virgin mobile"]
  - category: Ocio
    contains: ["netflix", "spotify", "cine", "hbo", "disney+", "prime video"]
  - category: Deporte
    contains: ["gimnasio", "gym", "piscina", "natacion", "natación"]
  - category: Salud
    contains: ["farmacia", "clinica", "clínica", "seguro medico", "seguro médico"]
  - category: Compras
    contains: ["amazon", "wallapop"]
  - category: Hogar
    contains: ["ferreteria", "ferretería", "leroy merlin", "bricodepot", "bricomart"]
  - category: Educación
    contains: ["colegio", "academia", "universidad", "escuela", "libreria", "librería"]
  - category: Seguros
    contains: ["seguro", "aseguradora"]
  - category: Impuestos
    contains: []
  - category: Nómina/Ingresos
    contains: ["nomina", "nómina"]
  - category: Transferencias
    contains: ["traspaso", "transferencia", "bizum"]
  - category: Efectivo
    contains: ["reintegro"]
  - category: Finanzas
    contains: ["prestamo", "liquidacion"]
"""


def ensure_rules_file(path: str) -> None:
    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_RULES, encoding="utf-8")


def load_rules(path: str) -> list[dict[str, Any]]:
    """Blocking. Returns [] (and logs) if the file is missing or malformed."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as err:
        _LOGGER.warning("Could not read %s: %s", path, err)
        return []
    rules = data.get("rules") or []
    compiled = []
    for rule in rules:
        category = rule.get("category")
        if not category:
            continue
        entry: dict[str, Any] = {"category": category}
        if rule.get("contains"):
            entry["contains"] = [str(s).lower() for s in rule["contains"]]
        if rule.get("regex"):
            try:
                entry["regex"] = re.compile(rule["regex"], re.IGNORECASE)
            except re.error as err:
                _LOGGER.warning("Invalid regex in categories.yaml for %r: %s", category, err)
        if rule.get("mcc"):
            entry["mcc"] = {str(m) for m in rule["mcc"]}
        compiled.append(entry)
    return compiled


def match_category(rules: list[dict[str, Any]], counterparty: str | None, remittance: str | None, mcc: str | None) -> str | None:
    text = f"{counterparty or ''} {remittance or ''}"
    text_lower = text.lower()
    for rule in rules:
        if "mcc" in rule and mcc in rule["mcc"]:
            return rule["category"]
        if "contains" in rule and any(term in text_lower for term in rule["contains"]):
            return rule["category"]
        if "regex" in rule and rule["regex"].search(text):
            return rule["category"]
    return None
