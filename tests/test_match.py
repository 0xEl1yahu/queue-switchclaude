from qsc import match, store


def tasks(*texts):
    s = store.Store("s"); return [s.new_task(t, [], None) for t in texts]


# Plan tests (Task 3).

def test_pick_clear_subject_reference():
    ts = tasks("Add a Stripe webhook handler", "Refactor the billing service")
    assert match.pick("the webhook handler should verify Stripe signatures", ts).id == "p1"   # g23


def test_pick_by_subject_word_tests():
    ts = tasks("Add a /login endpoint that returns a JWT", "Write tests for /login")
    assert match.pick("for the tests, include a rate-limit case", ts).id == "p2"              # g14


def test_pick_refuses_small_margin():
    ts = tasks("Write unit tests for billing", "Write unit tests for login")
    assert match.pick("for the tests, add a rate-limit case", ts) is None                       # review focus 2


def test_pick_refuses_low_score():
    ts = tasks("Add a Stripe webhook handler")
    assert match.pick("bump fastapi to the latest version", ts) is None                         # g25 is not an amend


def test_candidates_shape():
    ts = tasks("a b c", "d e f")
    c = match.candidates("a b", ts)
    assert c[0]["id"] == "p1" and set(c[0]) == {"id", "text", "score"} and len(c) == 2


# Additional tests beyond the plan's five.

def test_score_is_bounded_and_empty_text_scores_zero():
    assert 0.0 <= match.score("the webhook handler", "Add a Stripe webhook handler") <= 1.0
    assert match.score("", "") == 0.0 and match.score("anything", "") == 0.0 and match.score("", "anything") == 0.0
    assert match.score("add login", "add login") == 1.0


def test_score_ignores_case_quotes_and_whitespace():
    assert match.score("Add  the ‘Webhook’ handler.", "add the 'webhook' handler") == 1.0


def test_rank_sorts_descending_and_keeps_creation_order_on_ties():
    ts = tasks("Refactor the billing service", "Add a Stripe webhook handler", "unrelated thing")
    ranked = match.rank("the webhook handler should verify Stripe signatures", ts)
    assert [t.id for t, _ in ranked][0] == "p2"
    assert all(ranked[i][1] >= ranked[i + 1][1] for i in range(len(ranked) - 1))
    tied = match.rank("zzz", tasks("same text", "same text"))
    assert [t.id for t, _ in tied] == ["p1", "p2"] and tied[0][1] == tied[1][1]


def test_pick_empty_list_and_single_task():
    assert match.pick("anything", []) is None
    only = tasks("Add a Stripe webhook handler")
    assert match.pick("the webhook handler should verify Stripe signatures", only).id == "p1"


def test_pick_honours_explicit_thresholds():
    ts = tasks("Write unit tests for billing", "Write unit tests for login")
    msg = "for the tests, add a rate-limit case"
    assert match.pick(msg, ts, accept=0.0, margin=0.0) is not None       # ties pass a zero margin
    ts2 = tasks("Add a Stripe webhook handler")
    assert match.pick("bump fastapi to the latest version", ts2, accept=0.0) is not None
    assert match.pick("the webhook handler should verify Stripe signatures", ts2, accept=1.01) is None


def test_pick_with_running_task_among_candidates_g14_full():
    # The real g14 candidate list: the running /login endpoint, the queued user model and the tests.
    ts = tasks("Add a /login endpoint that returns a JWT", "Add a User model with hashed passwords", "Write tests for /login")
    assert match.pick("for the tests, include a rate-limit case", ts).id == "p3"


def test_pick_scenario_h_goodbye_script():
    ts = tasks("write a python script hello.py that prints hello, then run it", "add a goodbye.py that prints goodbye")
    assert match.pick("the goodbye script should also print the date", ts).id == "p2"


def test_pick_refuses_when_no_task_is_named():
    ts = tasks("Add a Stripe webhook handler", "Write tests for /login")
    assert match.pick("use bcrypt for the hashing", ts) is None


def test_candidates_limit_order_and_rounding():
    ts = tasks("a b c", "d e f", "a b x", "a b", "q")
    c = match.candidates("a b", ts, n=3)
    assert len(c) == 3 and c[0]["id"] == "p4" and all(c[i]["score"] >= c[i + 1]["score"] for i in range(2))
    assert all(isinstance(x["score"], float) and x["text"] for x in c)
    assert match.candidates("a b", []) == [] and len(match.candidates("a b", ts, n=0)) == 0


def test_non_string_message_fails_open():
    ts = tasks("Add a Stripe webhook handler")
    assert match.pick(None, ts) is None and match.candidates(None, ts)[0]["id"] == "p1"
