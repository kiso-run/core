"""Localization strings and language markers for worker notifications."""

from __future__ import annotations

# ISO 639-1 → English name (for "Answer in {lang}." prefix)
LANG_NAMES: dict[str, str] = {
    "en": "English",
    "it": "Italian",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
}

LANG_MARKERS: dict[str, set[str]] = {
    "it": {"vai", "apri", "cerca", "installa", "fammi", "dimmi", "controlla", "scrivi", "naviga"},
    "es": {"abre", "busca", "instala", "dime", "haz", "escribe", "navega", "muestra"},
    "fr": {"ouvre", "cherche", "installe", "montre", "fais", "écris", "navigue"},
    "de": {"öffne", "suche", "installiere", "zeige", "schreibe", "navigiere"},
    "pt": {"abra", "busque", "instale", "mostre", "escreva", "navegue"},
}

REPLAN_TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        "investigating": "Investigating... ({depth}/{max})",
        "replanning": "Replanning (attempt {depth}/{max}): {reason}",
        "stuck": (
            "I'm having trouble with this request. "
            "I've tried replanning {depth} times but keep hitting "
            "the same issue: {reason}\n"
            "Previous attempts: {tried}\n"
            "Can you help me with more details or a different approach?"
        ),
    },
    "it": {
        "investigating": "Indagine in corso... ({depth}/{max})",
        "replanning": "Ripianificazione (tentativo {depth}/{max}): {reason}",
        "stuck": (
            "Sto avendo difficoltà con questa richiesta. "
            "Ho riprovato {depth} volte ma continuo a riscontrare "
            "lo stesso problema: {reason}\n"
            "Tentativi precedenti: {tried}\n"
            "Puoi aiutarmi con più dettagli o un approccio diverso?"
        ),
    },
    "es": {
        "investigating": "Investigando... ({depth}/{max})",
        "replanning": "Replanificando (intento {depth}/{max}): {reason}",
        "stuck": (
            "Estoy teniendo dificultades con esta solicitud. "
            "He replanificado {depth} veces pero sigo encontrando "
            "el mismo problema: {reason}\n"
            "Intentos anteriores: {tried}\n"
            "¿Puedes ayudarme con más detalles o un enfoque diferente?"
        ),
    },
    "fr": {
        "investigating": "Investigation en cours... ({depth}/{max})",
        "replanning": "Replanification (tentative {depth}/{max}) : {reason}",
        "stuck": (
            "J'ai du mal avec cette demande. "
            "J'ai replanifié {depth} fois mais je rencontre toujours "
            "le même problème : {reason}\n"
            "Tentatives précédentes : {tried}\n"
            "Pouvez-vous m'aider avec plus de détails ou une approche différente ?"
        ),
    },
    "de": {
        "investigating": "Untersuchung läuft... ({depth}/{max})",
        "replanning": "Neuplanung (Versuch {depth}/{max}): {reason}",
        "stuck": (
            "Ich habe Schwierigkeiten mit dieser Anfrage. "
            "Ich habe {depth} Mal neu geplant, stoße aber immer wieder auf "
            "dasselbe Problem: {reason}\n"
            "Vorherige Versuche: {tried}\n"
            "Können Sie mir mit mehr Details oder einem anderen Ansatz helfen?"
        ),
    },
    "pt": {
        "investigating": "Investigando... ({depth}/{max})",
        "replanning": "Replanejando (tentativa {depth}/{max}): {reason}",
        "stuck": (
            "Estou tendo dificuldades com esta solicitação. "
            "Tentei replanejar {depth} vezes mas continuo encontrando "
            "o mesmo problema: {reason}\n"
            "Tentativas anteriores: {tried}\n"
            "Pode me ajudar com mais detalhes ou uma abordagem diferente?"
        ),
    },
}
