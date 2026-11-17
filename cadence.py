"""Motor de cadência: janelas de envio, dias da semana, intervalo entre contatos e tetos por período.

Não há aleatoriedade/"jitter" para simular comportamento humano: a cadência é determinística, previsível e
auditável. O objetivo é organização comercial e respeito aos limites da plataforma — não evasão de detecção.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from .config import cfg

DEFAULT_CADENCE = {
    "timezone": "America/Sao_Paulo",
    "window": {"start": "09:00", "end": "18:00", "days": [0, 1, 2, 3, 4]},  # 0=segunda
    "interval_seconds": 600,
    "max_contacts_per_day": 100,
    "max_contacts_per_hour": None,
    "max_messages_per_lead": 5,
}
INTERVAL_PRESETS = {"5min": 300, "10min": 600, "30min": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "24h": 86400}


def tz_of(cad) -> ZoneInfo:
    return ZoneInfo(cad.get("timezone") or cfg.TIMEZONE)


def _hm(s):
    h, m = s.split(":")
    return int(h), int(m)


def validate(cad):
    errs = []
    try:
        sh, sm = _hm(cad["window"]["start"]); eh, em = _hm(cad["window"]["end"])
        if (sh, sm) >= (eh, em):
            errs.append("horário final deve ser maior que o inicial")
    except Exception:
        errs.append("janela de horário inválida (use HH:MM)")
    if not cad.get("window", {}).get("days"):
        errs.append("selecione ao menos um dia da semana")
    if int(cad.get("interval_seconds", 0)) < 1:
        errs.append("intervalo deve ser maior que zero")
    try:
        tz_of(cad)
    except Exception:
        errs.append("fuso horário inválido")
    return errs


def next_allowed(t: dt.datetime, cad) -> dt.datetime:
    """Primeiro instante >= t dentro da janela (dias + horário) no fuso da campanha."""
    tz = tz_of(cad)
    win = cad["window"]
    sh, sm = _hm(win["start"]); eh, em = _hm(win["end"])
    local = t.astimezone(tz)
    for add in range(0, 15):
        d = (local + dt.timedelta(days=add)).date()
        if d.weekday() not in win["days"]:
            continue
        start = dt.datetime.combine(d, dt.time(sh, sm), tzinfo=tz)
        end = dt.datetime.combine(d, dt.time(eh, em), tzinfo=tz)
        if add == 0:
            if local >= end:
                continue
            cand = max(local, start)
        else:
            cand = start
        if cand < end:
            return cand.astimezone(dt.timezone.utc)
    raise ValueError("nenhuma janela de envio disponível")


def in_window(t, cad) -> bool:
    return abs((next_allowed(t, cad) - t).total_seconds()) < 1


def day_bounds_utc(t, cad):
    tz = tz_of(cad)
    local = t.astimezone(tz)
    s = dt.datetime.combine(local.date(), dt.time(0, 0), tzinfo=tz)
    return s.astimezone(dt.timezone.utc), (s + dt.timedelta(days=1)).astimezone(dt.timezone.utc)


class SlotAllocator:
    """Distribui contatos no tempo: intervalo fixo entre contatos, janela e tetos por hora/dia."""

    def __init__(self, cad, start, sent_today=0):
        self.cad = cad
        self.cur = start
        self.interval = dt.timedelta(seconds=int(cad["interval_seconds"]))
        self.day_cap = cad.get("max_contacts_per_day")
        self.hour_cap = cad.get("max_contacts_per_hour")
        self.days = {}
        self.hours = {}
        self.sent_today = sent_today
        self.tz = tz_of(cad)
        self._first = True
        self._today_key = start.astimezone(self.tz).date()

    def next(self) -> dt.datetime:
        cur = self.cur
        for _ in range(100000):
            t = next_allowed(cur, self.cad)
            local = t.astimezone(self.tz)
            dkey, hkey = local.date(), (local.date(), local.hour)
            used_day = self.days.get(dkey, 0) + (self.sent_today if dkey == self._today_key else 0)
            if self.day_cap and used_day >= self.day_cap:
                nxt = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(0, 0), tzinfo=self.tz)
                cur = nxt.astimezone(dt.timezone.utc)
                continue
            if self.hour_cap and self.hours.get(hkey, 0) >= self.hour_cap:
                nxt = local.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
                cur = nxt.astimezone(dt.timezone.utc)
                continue
            self.days[dkey] = self.days.get(dkey, 0) + 1
            self.hours[hkey] = self.hours.get(hkey, 0) + 1
            self.cur = t + self.interval
            return t
        raise RuntimeError("não foi possível alocar horário")
