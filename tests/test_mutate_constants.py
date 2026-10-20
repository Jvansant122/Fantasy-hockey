"""Hunt 14 mutate stream: the researched direction of rating constants whose own tests compare against a formula built
from the same constant (so zeroing or flipping it still passes) or set their own values (copy into tests/).
A deliberate change of direction should edit this table with the findings section that backs it."""
import rating

R = rating


def test_goalie_start_constants_keep_their_researched_direction():
    # findings 196: a long team break lifts whoever started last, a week's break more than 4-6 days
    assert R.BREAK_LOGITS[0][0] > R.BREAK_LOGITS[1][0] and R.BREAK_LOGITS[0][1] > R.BREAK_LOGITS[1][1] > 0
    assert R.AFTER_NO1_REST_OTHER_LOGIT < 0 and R.ROTATION_LATER_LOGIT < 0 and R.THREE_GOALIE_IDLE_LOGIT < 0
    assert R.ROTATION_THIRD_LOGIT < 0 and R.PLAIN_SECOND_LOGIT < 0
    assert R.PATTERN_LOGITS[(3, True)] > 0 > R.PATTERN_LOGITS[(3, False)] > R.PATTERN_LOGITS[(4, False)]
    assert R.EARLY_NO_START_LOGIT < 0 and R.NO1_ORDINARY_LOGIT > 0 and 0 < R.EVEN_TANDEM_B2B_SCALE < 1  # 450, 363, 451
    assert R.B2B_NIGHT2_NO1 < 0 < R.B2B_NIGHT2_OTHER and R.RELIEF_RARE_LOGIT > 0 and R.LOW_GA_WIN_LOGIT > 0
    assert R.WIN_LOGIT > 0 > R.LOSS_LOGIT and R.NEW_TEAM_START_LOGIT > 0
    assert R.NO_DECISION_LOGIT < min(R.LOSS_LOGITS.values())  # findings 163: a no-decision exit cuts more than any loss
    assert 0.5 < R.LIKELY_START_FLOOR < 1 and R.RESTED_NO1_LOGIT > 0  # handoffs 617, 363 (rested no. 1)
    assert 0 < R.INJURY_DRESS["DAY_TO_DAY"] < 1 and R.INJURY_DRESS["OUT"] == 0


def test_goalie_season_constants_keep_their_researched_direction():
    assert 0 < R.NO_DECISION_SEASON < 1 and 0.5 < R.PRIOR_TOP_CAP < 1 and 0 < R.INJURED_TAKER_WEIGHT < 1
    assert R.SAVE_GAP_SEASON[0] >= 0 and R.SAVE_GAP_SEASON[1] > 0  # findings 330 (1)
    assert 0 < R.GOALIE_SEASON_FLOOR < R.GOALIE_TAKER_MIN < 1 and R.PULLED_SEASON > 0  # handoffs 446, 272
    assert R.GOALIE_AGE_SEASON > 0 and R.SEASON_SHARE_K > 0  # handoff 640


def test_skater_season_constants_keep_their_researched_direction():
    assert R.PRESEASON_INJURED["short"] > R.PRESEASON_INJURED["long"] > R.PRESEASON_INJURED["season"] == 0  # findings 74
    assert R.DURABLE_OUT[0] > 0 > R.DURABLE_OUT[1]  # findings 330 (4)
    assert 0 < R.BOOST_DRESS_W < 1 and R.BOOST_FPG_BLEND[0] > 0 > R.BOOST_FPG_BLEND[1]
    assert 0.9 < R.OFFSEASON_MOVER_MULT < 1 and R.NEWS_STALE_DAYS > 0
    assert R.SEASON_TOI_CHG_C < 0 < R.SEASON_TOI_CHG_CAP and R.MISSING_REG_C < 0  # handoffs 507, 512, 509
    assert R.MISSING_REG_MIN_TG >= 1  # findings 402 tested teams with 3+ games: none before his team has played (509)
    assert R.NEWS_SURGERY < 0 and R.NEWS_INDEFINITE < 0 and R.NEWS_SEASON_OUT < R.NEWS_SURGERY
    assert R.NEWS_CALLED_UP[0] < 0 < R.NEWS_CALLED_UP[1]


def test_week_skater_constants_keep_their_researched_direction():
    assert R.NEXT_UP_PP_D_FPG > 0  # findings 310 (5)
    assert 0 < R.OUT_TODAY_CAP < R.OUT_LEFT_EARLY_CAP < R.SKATER_INJURY_CAP["OUT"]  # findings 176, 235
    assert [c for _, c in R.DTD_PLAYED_CAPS] == sorted(c for _, c in R.DTD_PLAYED_CAPS)  # more minutes, higher cap (630)
    assert R.CLEARED_LOGIT > 0 and R.CLEARED_SEASON_LOGIT > 0 and R.DFO_LINEUP_NEXT > 0.5 and R.NEW_TEAM_LOGIT > 0
    assert R.WEEK_HOME_AFTER_TRIP[0] > 0 > R.WEEK_HOME_AFTER_TRIP[1] and R.WEEK_OUT_LONG > 0  # handoff 500
    assert 0 < R.BOOST_WEEK_W <= 1 and R.OPP_LAST_C > 0


def test_likely_start_floor_matches_what_the_page_says():
    """Handoff 617: the page tells readers a Daily Faceoff "Likely" starter counts as at least 90%; the floor must say the same."""
    from conftest import ROOT
    page = (ROOT / "index.html").read_text()
    pct = f"at least {round(R.LIKELY_START_FLOOR * 100)}%"
    assert "likely ones " + pct in page and "counted as " + pct + " to start" in page
