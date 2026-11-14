import json
from signalpost.net import Response

ORG = "923609016"
RECORD = {"organisasjonsnummer": ORG, "navn": "FJORD TEST AS", "organisasjonsform": {"kode": "AS"},
          "antallAnsatte": 12, "konkurs": False, "underAvvikling": False,
          "forretningsadresse": {"adresse": ["Testveien 12"], "postnummer": "5003", "poststed": "BERGEN", "kommune": "BERGEN"},
          "naeringskode1": {"kode": "62.100", "beskrivelse": "Computer programming"}, "aktivitet": ["Builds business software."]}


def response(url, body, status=200, headers=None):
    raw = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return Response(url, status, raw.encode(), headers or {"content-type": "application/json; charset=utf-8"}, "2026-11-13T12:00:00Z")


def official_transport(url, headers):
    if url.endswith("/roller"):
        return response(url, {"rollegrupper": [{"roller": [{"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "person": {"navn": {"fornavn": "Ada", "etternavn": "Fjord"}}, "avregistrert": False}]}]})
    if "/regnskapsregisteret/" in url:
        return response(url, [{"regnskapstype": "SELSKAP", "virksomhet": {"organisasjonsnummer": ORG}, "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "valuta": "NOK", "resultatregnskapResultat": {"aarsresultat": 0, "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 1000000}}}}])
    if "/underenheter?" in url:
        return response(url, {"_embedded": {"underenheter": []}})
    return response(url, RECORD)
