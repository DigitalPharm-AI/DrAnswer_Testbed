"""Lightweight v1.3 contract test server.

This package is deliberately independent from the production Agent runtime.
It exists so the Backend and AI teams can integrate against the v1.3 wire
contract before either side's full implementation is available.

Import ``create_app`` from :mod:`contract_test_server.main`. Keeping package
initialization side-effect free prevents the callback worker from constructing
an unused API app or opening the database twice.
"""
