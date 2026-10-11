
from app.services.discovery import score_item

def test_keyword_scoring():
    score = score_item("Computer assistant with Excel and IT support", ["assistant", "computer", "IT support"])
    assert score > 0

def test_location_bonus():
    base = score_item("Assistant with computer skills", ["assistant", "computer"], "")
    with_location = score_item("Assistant with computer skills in Bengaluru", ["assistant", "computer"], "Bengaluru")
    assert with_location >= base
