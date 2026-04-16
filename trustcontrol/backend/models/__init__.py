# ════════════════════════════════════════════════════════════
#  Регистрация всех моделей для SQLAlchemy
#  Этот файл ОБЯЗАТЕЛЕН — без него init_db не создаст таблицы
# ════════════════════════════════════════════════════════════

from backend.models.user            import User            # noqa: F401
from backend.models.location        import Location        # noqa: F401
from backend.models.report          import Report          # noqa: F401
from backend.models.alert           import Alert           # noqa: F401
from backend.models.shift           import Shift           # noqa: F401
from backend.models.payment         import Payment         # noqa: F401
from backend.models.pos_transaction import PosTransaction  # noqa: F401
from backend.models.failed_job      import FailedJob       # noqa: F401

__all__ = ["User", "Location", "Report", "Alert", "Shift", "Payment",
           "PosTransaction", "FailedJob"]
