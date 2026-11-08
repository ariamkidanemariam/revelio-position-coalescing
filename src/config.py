import os
import re
from datetime import date


def _env_number(name, default, kind):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return kind(raw)
    except (ValueError, TypeError):
        return raw


EARLIEST_PLAUSIBLE_DATE = "1940-01-01"
MAX_DAYS_AFTER_OBSERVATION = 366

PLACEHOLDER_VALUES = (
    "null", "none", "n/a", "nan", "undefined", "unknown", "not applicable",
    "无", "無", "暂无", "なし", "없음", "нет", "لا يوجد",
)
COMPANY_SUFFIXES = (
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "ag", "sa", "sas", "sarl", "srl", "spa",
    "bv", "nv", "ab", "oy", "as", "pvt", "pte", "lp", "llp", "pc",
)
COMPANY_SUFFIXES_NO_SPACES = ("股份有限公司", "有限责任公司", "有限公司", "公司", "株式会社")
COMPANY_PREFIXES = ("شركة", "مؤسسة")

START_DATE_TOLERANCE_DAYS = 366
COMPANY_SIMILARITY = 0.85
TITLE_SIMILARITY = 0.85

ALLOW_COMPANY_RENAME = os.environ.get("ALLOW_COMPANY_RENAME", "1") == "1"
ALLOW_TITLE_EDIT = os.environ.get("ALLOW_TITLE_EDIT", "1") == "1"
ALLOW_LEVEL_EDIT = os.environ.get("ALLOW_LEVEL_EDIT", "1") == "1"


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def available_cpus(cgroup_root="/sys/fs/cgroup"):
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 4

    quota = _read(os.path.join(cgroup_root, "cpu.max"))
    if quota and not quota.startswith("max"):
        limit, period = (int(x) for x in quota.split()[:2])
        cpus = min(cpus, max(1, limit // period))
    else:
        for folder in ("cpu", "cpu,cpuacct"):
            limit = _read(os.path.join(cgroup_root, folder, "cpu.cfs_quota_us"))
            period = _read(os.path.join(cgroup_root, folder, "cpu.cfs_period_us"))
            if limit and period and limit.lstrip("-").isdigit() and int(limit) > 0 and int(period) > 0:
                cpus = min(cpus, max(1, int(limit) // int(period)))
                break
    return max(1, cpus)


def available_memory_gb():
    limits = []
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        value = _read(path)
        if value and value.isdigit() and int(value) < 1 << 60:
            limits.append(int(value))
    try:
        limits.append(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        pass
    return min(limits) / 1024**3 if limits else 16.0


THREADS = _env_number("THREADS", available_cpus(), int)
MEMORY_LIMIT = os.environ.get("MEMORY_LIMIT", f"{max(1, min(10, int(available_memory_gb() * 0.6)))}GB")
WORKER_MEMORY_LIMIT = os.environ.get("WORKER_MEMORY_LIMIT", "1GB")
WORK_DIR = os.environ.get("WORK_DIR", "/tmp/coalesce_work")
LARGE_USER_VERSIONS = _env_number("LARGE_USER_VERSIONS", 1000, int)


def matching_workers(threads, memory_gb):
    return max(1, min(threads, int(memory_gb // 2)))


MATCH_WORKERS = matching_workers(THREADS, available_memory_gb()) if isinstance(THREADS, int) else 1


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MEMORY = re.compile(r"^\d+(\.\d+)?\s*(KB|MB|GB|TB|KIB|MIB|GIB|TIB)$", re.IGNORECASE)


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and value not in (float("inf"), float("-inf")))


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate():
    problems = []

    def check(ok, message):
        if not ok:
            problems.append(message)

    check(_is_int(THREADS) and THREADS >= 1, f"THREADS must be a whole number >= 1, got {THREADS!r}")
    check(_is_int(LARGE_USER_VERSIONS) and LARGE_USER_VERSIONS >= 1,
          f"LARGE_USER_VERSIONS must be a whole number >= 1, got {LARGE_USER_VERSIONS!r}")
    for name, value in (("MEMORY_LIMIT", MEMORY_LIMIT), ("WORKER_MEMORY_LIMIT", WORKER_MEMORY_LIMIT)):
        check(isinstance(value, str) and bool(_MEMORY.match(value.strip())),
              f"{name} must look like '10GB' or '512MB', got {value!r}")

    valid_date = isinstance(EARLIEST_PLAUSIBLE_DATE, str) and bool(_DATE.match(EARLIEST_PLAUSIBLE_DATE))
    if valid_date:
        try:
            date.fromisoformat(EARLIEST_PLAUSIBLE_DATE)
        except ValueError:
            valid_date = False
    check(valid_date, f"EARLIEST_PLAUSIBLE_DATE must be a real date written YYYY-MM-DD, "
                      f"got {EARLIEST_PLAUSIBLE_DATE!r}")

    check(_is_int(MAX_DAYS_AFTER_OBSERVATION) and MAX_DAYS_AFTER_OBSERVATION >= 0,
          f"MAX_DAYS_AFTER_OBSERVATION must be a whole number >= 0, got {MAX_DAYS_AFTER_OBSERVATION!r}")
    check(_is_int(START_DATE_TOLERANCE_DAYS) and START_DATE_TOLERANCE_DAYS >= 0,
          f"START_DATE_TOLERANCE_DAYS must be a whole number >= 0, got {START_DATE_TOLERANCE_DAYS!r}")
    for name, value in (("COMPANY_SIMILARITY", COMPANY_SIMILARITY), ("TITLE_SIMILARITY", TITLE_SIMILARITY)):
        check(_is_number(value) and 0 < value <= 1, f"{name} must be between 0 and 1, got {value!r}")

    for name, value in (("ALLOW_COMPANY_RENAME", ALLOW_COMPANY_RENAME), ("ALLOW_TITLE_EDIT", ALLOW_TITLE_EDIT),
                        ("ALLOW_LEVEL_EDIT", ALLOW_LEVEL_EDIT)):
        check(isinstance(value, bool), f"{name} must be True or False, got {value!r}")

    return problems
