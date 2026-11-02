from tests.test_kyc import _payload, _post
from tests.test_refunds import _submit, tx_id  # noqa: F401


def test_kyc_queue_sorts_by_applicant(login, anon):
    for i, name in enumerate(["Zed Last", "Amy First"]):
        payload = _payload(ref=f"s{i}", email=f"s{i}@example.com", fp=f"fp_s{i}", decision="review")
        payload["applicant"]["name"] = name
        assert _post(anon, payload).status_code == 201
    april = login("april")
    html = april.get("/kyc?status=pending&sort=applicant&dir=asc").text
    assert html.index("Amy First") < html.index("Zed Last")
    html = april.get("/kyc?status=pending&sort=applicant&dir=desc").text
    assert html.index("Zed Last") < html.index("Amy First")
    assert 'href="/kyc?status=pending&amp;sort=applicant&amp;dir=asc"' in html
    assert "↓" in html and "&amp;darr;" not in html and "&darr;" not in html


def test_kyc_queue_ignores_unknown_sort(login):
    assert login("april").get("/kyc?sort=drop_table&dir=sideways").status_code == 200


def test_refunds_requests_view_shows_route(login, tx_id):  # noqa: F811
    oscar = login("oscar")
    _submit(oscar, tx_id, "12.00")
    html = oscar.get("/refunds?view=requests&sort=amount&dir=desc").text
    assert "Route" in html and ">Auto<" in html
    assert 'href="/refunds?view=requests&amp;sort=amount&amp;dir=asc"' in html
    html = oscar.get("/refunds?sort=amount&dir=asc").text
    assert 'href="/refunds?sort=amount&amp;dir=desc"' in html


def test_flags_list_sorts(login):
    oscar = login("oscar")
    oscar.post("/flags", data={"key": "sort_me", "enabled": "true", "rollout_percent": "100"}, follow_redirects=False)
    html = oscar.get("/flags?sort=flag&dir=desc").text
    assert 'href="/flags?sort=flag&amp;dir=asc"' in html


def test_refunds_view_tabs_encode_search(login):
    html = login("oscar").get("/refunds", params={"q": "Smith & Jones"}).text
    assert 'href="/refunds?view=requests&amp;q=Smith+%26+Jones"' in html


def test_refunds_clear_keeps_view_drops_search(login):
    html = login("oscar").get("/refunds", params={"view": "requests", "q": "Smith"}).text
    assert 'href="/refunds?view=requests">Clear</a>' in html
