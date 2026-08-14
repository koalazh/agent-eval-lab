from request_pager import RequestPager


def test_first_request_has_targeted_page_sequence():
    pager = RequestPager(page_size=2)
    pager.start_request()
    assert [pager.next_page(), pager.next_page()] == [(0, 2), (2, 2)]


def test_each_new_request_starts_at_zero_in_the_full_suite():
    pager = RequestPager(page_size=2)
    pager.start_request()
    pager.next_page()
    pager.next_page()
    pager.start_request()
    assert pager.next_page() == (0, 2)
