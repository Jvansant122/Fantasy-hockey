"""A fake ESPN fantasy league for running scripts/fetch.py main() end to end (handoff 662, from hunt 14 audit3). NHL calls go to the repo's
own test fake (tests/test_add_ratings.FakeSession); ESPN fantasy calls are answered here; Daily Faceoff / odds / injuries
are leads.* stubs the scenario sets."""
import json
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import test_add_ratings as T
from test_add_ratings import Resp

ET = ZoneInfo("America/New_York")
SP1 = date(2026, 9, 29)  # ESPN 2027 period 1 (the live data's sp1_date)


def sp_of(d):
    return (d - SP1).days + 1


def day_of(sp):
    return SP1 + timedelta(days=sp - 1)


def ms(dt):
    return int(dt.timestamp() * 1000)


def player(pid, name, pro, pos, inj=None, gp=4, total=10.0, last=(70, 140.0), proj=(82, 150.0), goalie=False):
    stats = [{"statSourceId": 0, "statSplitTypeId": 0, "seasonId": 2027, "stats": {"34": gp, "0": gp}, "appliedTotal": total},
             {"statSourceId": 0, "statSplitTypeId": 0, "seasonId": 2026, "stats": {"34": last[0], "0": last[0]}, "appliedTotal": last[1]},
             {"statSourceId": 1, "statSplitTypeId": 0, "seasonId": 2027, "stats": {"34": proj[0], "0": proj[0]}, "appliedTotal": proj[1]}]
    posid = {"C": 1, "LW": 2, "RW": 3, "D": 4, "G": 5}[pos]
    slots = {"C": [0, 3, 6, 7, 8], "LW": [1, 3, 6, 7, 8], "RW": [2, 3, 6, 7, 8], "D": [4, 6, 7, 8], "G": [5, 7, 8]}[pos]
    return {"id": pid, "fullName": name, "proTeamId": pro, "defaultPositionId": posid, "eligibleSlots": slots,
            "injuryStatus": inj or "ACTIVE", "injured": bool(inj), "ownership": {"percentOwned": 50.0}, "stats": stats}


PRO = {10: "Tor", 1: "Bos", 8: "Mtl"}  # ESPN proTeam ids
NHL_PRO = {"TOR": 10, "BOS": 1, "MTL": 8}


class League:
    """scenario knobs: today_sp (ESPN's day), period, weeks map, games per pro team (set of sps), start hour ET, rosters,
    free agents, transactions, moves used."""

    def __init__(self, today_sp, period=None, periods=None, games=None, start_et=19, rosters=None, fas=None, tx=None,
                 moves=None, slot_counts=None, drop_from_schedule=(), scoreboard=None, double=()):
        self.today_sp = today_sp
        self.periods = periods or {str(k): [k] for k in range(1, 23)} | {"23": [23, 24], "24": [25, 26]}
        self.period = period or next((int(k) for k, v in self.periods.items()
                                     if any(sp in (range(1, 7) if w == 1 else range(7 * w - 7, 7 * w)) for w in v for sp in [today_sp])), max(int(k) for k in self.periods))
        self.games = games or {10: set(range(3, 200, 2)), 1: set(range(3, 200, 2)), 8: set(range(4, 200, 3))}
        self.start_et = start_et
        self.rosters = rosters
        self.fas = fas or []
        self.tx = tx or []
        self.moves = moves or {}
        self.slot_counts = slot_counts or {"0": 2, "1": 2, "2": 2, "3": 3, "4": 5, "5": 2, "6": 1, "7": 5, "8": 2}
        self.drop = set(drop_from_schedule)
        self.double = set(double)
        self.calls = []

    def pro_teams(self):
        out = []
        for tid, ab in PRO.items():
            g = {}
            for sp in sorted(self.games[tid]):
                if (tid, sp) in self.drop:
                    continue
                st = datetime(*day_of(sp).timetuple()[:3], self.start_et, tzinfo=ET)
                g[str(sp)] = [{"id": sp * 100 + min(tid, 9), "date": ms(st)}]
                if (tid, sp) in self.double:
                    g[str(sp)].append({"id": sp * 100 + 50 + min(tid, 9), "date": ms(st + timedelta(hours=3))})
            out.append({"id": tid, "abbrev": ab, "proGamesByScoringPeriod": g})
        out.append({"id": 0, "abbrev": "FA", "proGamesByScoringPeriod": {}})
        return out

    def league(self):
        teams = []
        for tid, entries in self.rosters.items():
            teams.append({"id": tid, "name": f"Team {tid}", "owners": [], "primaryOwner": "",
                          "roster": {"entries": [{"playerId": p["id"], "lineupSlotId": slot, "playerPoolEntry": {"player": p}}
                                                 for p, slot in entries]},
                          "transactionCounter": {"matchupAcquisitionTotals": self.moves.get(tid, {})}, "waiverRank": tid})
        return {"status": {"currentMatchupPeriod": self.period, "latestScoringPeriod": self.today_sp},
                "scoringPeriodId": self.today_sp, "teams": teams,
                "settings": {"scheduleSettings": {"matchupPeriods": self.periods},
                             "acquisitionSettings": {"matchupAcquisitionLimit": 1, "matchupLimitPerScoringPeriod": True},
                             "rosterSettings": {"lineupSlotCounts": self.slot_counts, "lineupSlotStatLimits": {}},
                             "tradeSettings": {"deadlineDate": 0}}}

    def all_players(self):
        return [p for e in self.rosters.values() for p, _ in e] + [p for p, _ in self.fas]

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        views = [v for k, v in (params or []) if k == "view"] if isinstance(params, list) else []
        filt = json.loads((headers or {}).get("X-Fantasy-Filter") or "{}")
        if "proTeamSchedules_wl" in views:
            return Resp({"settings": {}} if getattr(self, "no_pro", False) else {"settings": {"proTeams": self.pro_teams()}})
        if "mTransactions2" in views:
            return Resp({"transactions": self.tx})
        if "kona_player_info" in views:
            pf = filt.get("players", {})
            fas = self.fas
            if (pf.get("filterSlotIds") or {}).get("value") == [5]:
                fas = [(p, s) for p, s in fas if p["defaultPositionId"] == 5]
            return Resp({"players": [{"player": p, "status": s} for p, s in fas]})
        if "kona_playercard" in views:
            ids = set(filt["players"]["filterIds"]["value"])
            return Resp({"players": [{"player": p} for p in self.all_players() if p["id"] in ids]})
        if "mMatchupScore" in views:
            ids = list(self.rosters)
            return Resp({"schedule": [{"matchupPeriodId": self.period, "home": {"teamId": ids[0], "totalPoints": 12.0},
                                       "away": {"teamId": ids[1], "totalPoints": 10.0}}]})
        if "mTeam" in views:
            return Resp(self.league())
        raise AssertionError(f"unexpected ESPN call {url} {views}")


class NHL(T.FakeSession):
    """The repo's NHL fake, with a schedule for any Monday (the fixed week for Oct 5, empty otherwise) and BOS-TOR games
    on the given dates."""

    def __init__(self, week_dates=None, ppd=(), **kw):
        super().__init__(**kw)
        self.week_dates, self.ppd = week_dates, set(ppd)

    def get(self, url, params=None, headers=None, timeout=None):
        m = re.search(r"/schedule/(\d{4}-\d{2}-\d{2})$", url)
        if m:
            mon = date.fromisoformat(m.group(1))
            dates = [d for d in (T.WEEK if self.week_dates is None else self.week_dates) if mon <= date.fromisoformat(d) < mon + timedelta(days=7)]
            return Resp({"gameWeek": [{"date": d, "games": [{"gameType": 2, "awayTeam": {"abbrev": "BOS"}, "homeTeam": {"abbrev": "TOR"},
                                                             "startTimeUTC": f"{d}T23:00:00Z",
                                                             "gameScheduleState": "PPD" if d in self.ppd else "OK"}]} for d in dates]})
        return super().get(url, params, headers, timeout)


class Combined:
    def __init__(self, league, nhl):
        self.league, self.nhl = league, nhl
        self.headers, self.cookies = {}, type("C", (), {"set": lambda *a: None})()

    def mount(self, *a):
        pass

    def get(self, url, params=None, headers=None, timeout=None):
        if "site.api.espn.com" in url and url.endswith("/teams"):
            return Resp({"sports": [{"leagues": [{"teams": [{"team": {"id": str(t), "abbreviation": a.upper()}} for t, a in PRO.items()]}]}]})
        if "site.api.espn.com" in url and url.endswith("/scoreboard"):
            day = datetime.strptime(params["dates"], "%Y%m%d").date()
            sp = sp_of(day)
            evs = []
            by_sp = {}
            for t in PRO:
                if sp in self.league.games[t] and (t, sp) not in self.league.drop:
                    by_sp.setdefault(sp, []).append(t)
            if by_sp.get(sp):
                st = datetime(day.year, day.month, day.day, self.league.start_et, tzinfo=ET).astimezone(timezone.utc)
                ts = by_sp[sp]
                for i in range(0, len(ts) - 1, 2):
                    evs.append({"date": st.strftime("%Y-%m-%dT%H:%MZ"), "competitions": [{"competitors": [
                        {"team": {"id": str(ts[i]), "abbreviation": PRO[ts[i]].upper()}},
                        {"team": {"id": str(ts[i + 1]), "abbreviation": PRO[ts[i + 1]].upper()}}]}]})
            return Resp({"events": evs})
        if "fantasy.espn.com" in url:
            return self.league.get(url, params, headers, timeout)
        return self.nhl.get(url, params, headers, timeout)


def frozen(now_utc):
    class F(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_utc.astimezone(tz) if tz else now_utc.astimezone(ET).replace(tzinfo=None)
    return F
