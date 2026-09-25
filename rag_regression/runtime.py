"""Executed inside the task container; compatible with Python 3.6+."""

import importlib.util
import json
import os
import sys
import traceback
import unittest


def main():
    backend, test_path, output = sys.argv[1:4]
    result = {"cases": [], "collection_errors": [], "backend": backend}
    sys.path.insert(0, os.getcwd())
    if backend == "pytest":
        import pytest

        class Recorder:
            def pytest_runtest_logreport(self, report):
                result["cases"].append({"id": report.nodeid, "phase": report.when,
                    "outcome": report.outcome, "xfail": bool(getattr(report, "wasxfail", False)),
                    "detail": str(report.longrepr) if report.failed else ""})

            def pytest_collectreport(self, report):
                if report.failed:
                    result["collection_errors"].append(str(report.longrepr))

        code = int(pytest.main([test_path, "-q", "-o", "addopts="], plugins=[Recorder()]))
    else:
        class Recorder(unittest.TextTestResult):
            def record(self, test, outcome, error=None):
                phase = "call"
                if error:
                    names = {frame.name for frame in traceback.extract_tb(error[2])}
                    if names & {"setUp", "setUpClass", "setUpModule", "_callSetUp"}:
                        phase = "setup"
                    elif names & {"tearDown", "tearDownClass", "tearDownModule", "_callTearDown"}:
                        phase = "teardown"
                result["cases"].append({"id": test.id(), "phase": phase, "outcome": outcome,
                    "xfail": False, "detail": self._exc_info_to_string(error, test) if error else ""})

            def addSuccess(self, test):
                super().addSuccess(test)
                self.record(test, "passed")

            def addFailure(self, test, error):
                super().addFailure(test, error)
                self.record(test, "failed", error)

            def addError(self, test, error):
                super().addError(test, error)
                self.record(test, "failed", error)

            def addSkip(self, test, reason):
                super().addSkip(test, reason)
                self.record(test, "skipped")

            def addExpectedFailure(self, test, error):
                super().addExpectedFailure(test, error)
                self.record(test, "skipped", error)

            def addUnexpectedSuccess(self, test):
                super().addUnexpectedSuccess(test)
                self.record(test, "skipped")

            def addSubTest(self, test, subtest, error):
                super().addSubTest(test, subtest, error)
                self.record(subtest, "failed" if error else "passed", error)

        try:
            if backend == "django":
                sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
                os.environ.setdefault("DJANGO_SETTINGS_MODULE", "test_sqlite")
                import django
                django.setup()
            spec = importlib.util.spec_from_file_location("test_rag_regression", test_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            suite = unittest.defaultTestLoader.loadTestsFromModule(module)
            if backend == "django":
                from django.test.runner import DiscoverRunner

                class TextRunner(unittest.TextTestRunner):
                    resultclass = Recorder

                class DjangoRunner(DiscoverRunner):
                    test_runner = TextRunner

                    def build_suite(self, *args, **kwargs):
                        return suite

                code = 1 if DjangoRunner(verbosity=2, interactive=False).run_tests([]) else 0
            else:
                run = unittest.TextTestRunner(verbosity=2, resultclass=Recorder).run(suite)
                code = 0 if run.wasSuccessful() else 1
        except Exception:
            result["collection_errors"].append(traceback.format_exc())
            traceback.print_exc()
            code = 2
    result["exit_code"] = code
    with open(output, "w") as handle:
        json.dump(result, handle)
    return code


if __name__ == "__main__":
    sys.exit(main())
