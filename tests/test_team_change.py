"""Players whose ESPN team and NHL team disagree (handoff 501, hunt13/team_change/). The fake league: TOR and BOS played
each other on past dates; with bye=True this week TOR plays MTL on every week date and BOS has no game."""
import rating
from test_add_ratings import FakeSession, MONDAY, TODAY, espn


def run(monkeypatch, tmp_path, players, **fake):
    f = FakeSession(**fake)
    monkeypatch.setattr(rating.requests, "Session", lambda: f)
    monkeypatch.setattr(rating, "INJURY_LOG", tmp_path / "injury_log.jsonl")
    monkeypatch.setattr(rating, "load_news", lambda: {})
    moved = []

    def reschedule(p, team):  # fetch.py's: the new team's games this week (TOR 2, BOS none)
        moved.append((p["name"], team))
        p.update(team=team, games_left=2 if team == "TOR" else 0, games=2 if team == "TOR" else 0)
    rating.add_ratings(players, 2027, TODAY, MONDAY, [], reschedule=reschedule)
    return {p["name"]: p for p in players}, moved


def test_skater_the_nhl_moved_first_plays_his_new_teams_games(monkeypatch, tmp_path):
    """Traded TOR -> BOS and played 2 games for BOS while ESPN still lists TOR: he gets BOS's games (none this week),
    not TOR's 2."""
    out, moved = run(monkeypatch, tmp_path, [espn(103, "David Pastrnak", "TOR", "RW", games_left=2)], moved={3: 2}, bye=True)
    assert moved == [("David Pastrnak", "BOS")]
    assert out["David Pastrnak"]["cr_games"] == 0 and out["David Pastrnak"]["cr"] == 0


def test_skater_the_nhl_moved_first_keeps_his_new_teams_games(monkeypatch, tmp_path):
    """Traded BOS -> TOR and played 2 TOR games while ESPN still lists BOS (no game this week): TOR's 2 games count."""
    out, moved = run(monkeypatch, tmp_path, [espn(101, "Auston Matthews", "BOS", "C", games_left=0)], moved={1: 2}, bye=True)
    assert moved == [("Auston Matthews", "TOR")]
    assert out["Auston Matthews"]["cr_games"] > 1 and out["Auston Matthews"]["cr"] > 0


def test_goalie_on_another_nhl_roster_never_outstarts_his_games(monkeypatch, tmp_path):
    """On TOR's NHL roster but ESPN lists BOS (no game this week): his starts come from BOS's games, so expected starts
    stay within games left and his nights (check_data P3, G1, G6 blocked publishing)."""
    woll = espn(111, "Joseph Woll", "BOS", "G", games_left=0)
    stol = espn(112, "Anthony Stolarz", "TOR", "G", games_left=2)
    out, _ = run(monkeypatch, tmp_path, [woll, stol], bye=True)
    w = out["Joseph Woll"]
    assert w["cr_games"] <= w["games_left"] and w["cr_why"]["exp_starts"] <= w["games_left"]
    assert sum(w["cr_g_nights"].values()) <= 0.01
    assert out["Anthony Stolarz"]["cr_games"] > 1.5  # TOR's games go to the goalie left on its roster
