class RequestPager:
    def __init__(self, page_size: int = 2):
        self.page_size = page_size
        self._offset = 0

    def start_request(self) -> None:
        """Begin a new independent request."""
        pass

    def next_page(self) -> tuple[int, int]:
        offset = self._offset
        self._offset += self.page_size
        return offset, self.page_size
