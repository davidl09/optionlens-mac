def pytest_configure(config):
    config.addinivalue_line("markers", "perf: quick performance guard for the vectorized screeners")
