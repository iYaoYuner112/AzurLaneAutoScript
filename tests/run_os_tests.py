"""Run the repository's tests/os suite without pytest.

Usage:
    python tests/run_os_tests.py [test_file_or_dir]

It loads every test_*.py under tests/os (or the given file/directory), runs
plain test_* functions and unittest.TestCase classes, and exits non-zero on
failure.
"""
import importlib.util
import inspect
import io
import os
import sys
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / 'tests' / 'os'
if TESTS.is_file():
    TEST_DIR = TESTS.parent
    files = [TESTS.name]
else:
    TEST_DIR = TESTS
    files = sorted(f for f in os.listdir(TEST_DIR) if f.startswith('test_') and f.endswith('.py'))

sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

passed = 0
failed = 0
failures = []

for name in files:
    path = TEST_DIR / name
    mod_name = 'os_' + name[:-3]
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        failed += 1
        failures.append((name, '<import>', traceback.format_exc()))
        print('IMPORT FAIL', name)
        continue

    cases = []
    for attr, obj in sorted(vars(module).items()):
        if inspect.isfunction(obj) and attr.startswith('test_'):
            cases.append(attr)
        elif inspect.isclass(obj) and issubclass(obj, unittest.TestCase):
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(obj)
            stream = io.StringIO()
            result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
            passed += result.testsRun - len(result.failures) - len(result.errors)
            for case, tb in list(result.failures) + list(result.errors):
                failed += 1
                failures.append((name, str(case), tb))
                print('FAIL', name, case)

    for attr in cases:
        try:
            getattr(module, attr)()
            passed += 1
        except Exception:
            failed += 1
            failures.append((name, attr, traceback.format_exc()))
            print('FAIL', name, attr)

print('')
print('=' * 60)
print(f'passed={passed} failed={failed}')
for file_name, case, tb in failures:
    print('')
    print('-' * 60)
    print(f'{file_name} :: {case}')
    print(tb)

sys.exit(1 if failed else 0)
