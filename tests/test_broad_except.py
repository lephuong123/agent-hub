"""L8 (docs/dev-notes/plan-logging.md B4): quy tắc nuốt lỗi — `except Exception` phải log kèm exc_info, ném lại, hoặc có
`# fail-closed: …` / `# best-effort: …`. Baseline theo file ở scripts/broad_except_baseline.txt chỉ được GIẢM."""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_broad_except", ROOT / "scripts" / "check_broad_except.py")
assert _spec and _spec.loader
cbe = importlib.util.module_from_spec(_spec)
sys.modules["check_broad_except"] = cbe
_spec.loader.exec_module(cbe)


def bad(code: str) -> list[int]:
    return cbe.scan_source(textwrap.dedent(code))


def test_no_regression_against_baseline() -> None:
    found = cbe.scan()
    regress = cbe.regressions(found, cbe.read_baseline())
    assert not regress, "handler `except Exception` mới chưa log exc_info / chưa có `# fail-closed:`/`# best-effort:`:\n" + "\n".join(regress)


def test_check_mode_exit_code() -> None:
    assert cbe.main(["--check"]) == 0


@pytest.mark.parametrize("code", [
    # (a) log kèm exc_info
    """
    try:
        f()
    except Exception:
        log.debug("x", exc_info=True)
    """,
    """
    try:
        f()
    except Exception as e:
        self.log.warning("x %s", e, exc_info=e)
    """,
    """
    try:
        f()
    except Exception:
        log.exception("x")
    """,
    """
    try:
        f()
    except Exception:
        logging.exception("x")
    """,
    """
    try:
        f()
    except BaseException:
        if cond:
            logger.error("x", exc_info=True)
        return None
    """,
    # (b) ném lại
    """
    try:
        f()
    except Exception:
        cleanup()
        raise
    """,
    """
    try:
        f()
    except Exception as e:
        raise HubError("y") from e
    """,
    # (c) chú thích có lý do
    """
    try:
        f()
    except Exception:  # fail-closed: lỗi = deny
        return False
    """,
    """
    try:
        f()
    except Exception:
        pass  # best-effort: dọn dẹp
    """,
    """
    try:
        f()
    except Exception:  # pragma: no cover — Windows không có
        pass
    """,
    """
    try:
        f()
    except Exception:  # noqa: BLE001 - cấu hình hỏng
        pass
    """,
    """
    try:
        f()
    except Exception:
        # best-effort: chú thích dòng riêng trước lệnh đầu
        pass
    """,
    # không phải handler rộng
    """
    try:
        f()
    except (OSError, ValueError):
        pass
    """,
])
def test_compliant(code: str) -> None:
    assert bad(code) == []


@pytest.mark.parametrize("code", [
    """
    try:
        f()
    except Exception:
        pass
    """,
    """
    try:
        f()
    except:
        return None
    """,
    """
    try:
        f()
    except (ValueError, Exception):
        x = 1
    """,
    """
    try:
        f()
    except BaseException:
        pass
    """,
    # log KHÔNG có exc_info → chưa đạt
    """
    try:
        f()
    except Exception as e:
        log.warning("lỗi %s", e)
    """,
    # exc_info=False không tính
    """
    try:
        f()
    except Exception:
        log.debug("x", exc_info=False)
    """,
    # pragma trần / chú thích không theo mẫu → chưa đạt
    """
    try:
        f()
    except Exception:  # pragma: no cover
        pass
    """,
    """
    try:
        f()
    except Exception:  # noqa: BLE001
        pass
    """,
    """
    try:
        f()
    except Exception:  # best-effort:
        pass
    """,
    """
    try:
        f()
    except Exception:  # config hỏng không được chặn
        pass
    """,
    # raise / log trong hàm lồng không tính
    """
    try:
        f()
    except Exception:
        def g():
            raise ValueError
        cb(g)
    """,
    # chú thích ở dòng thứ hai của thân không tính
    """
    try:
        f()
    except Exception:
        a = 1
        b = 2  # best-effort: muộn quá
    """,
])
def test_non_compliant(code: str) -> None:
    assert len(bad(code)) == 1


def test_nested_handlers_counted_separately() -> None:
    code = """
    try:
        f()
    except Exception:
        try:
            g()
        except Exception:
            pass
        raise
    """
    assert bad(code) == [7]


def test_regressions_per_file(tmp_path: Path) -> None:
    found = {"a.py": [1, 2, 3], "b.py": [5], "c.py": [9]}
    base = {"a.py": 3, "b.py": 2}
    out = cbe.regressions(found, base)
    assert len(out) == 1 and out[0].startswith("c.py")
    out = cbe.regressions({"a.py": [1, 2, 3, 4]}, base)
    assert out and out[0].startswith("a.py: 4 > baseline 3")
    p = tmp_path / "bl.txt"
    cbe.write_baseline(found, p)
    assert cbe.read_baseline(p) == {"a.py": 3, "b.py": 1, "c.py": 1}
