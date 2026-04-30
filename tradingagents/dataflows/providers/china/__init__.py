"""
中国市场数据提供器
包含 A股、港股等中国市场的数据源
"""

# 导入 AKShare 提供器
try:
    from .akshare import AKShareProvider
    AKSHARE_AVAILABLE = True
except ImportError:
    AKShareProvider = None
    AKSHARE_AVAILABLE = False

# 导入 Tushare 提供器
try:
    from .tushare import TushareProvider
    TUSHARE_AVAILABLE = True
except ImportError:
    TushareProvider = None
    TUSHARE_AVAILABLE = False

# 导入 Baostock 提供器
try:
    from .baostock import BaostockProvider
    BAOSTOCK_AVAILABLE = True
except ImportError:
    BaostockProvider = None
    BAOSTOCK_AVAILABLE = False


# 导入东方财富 Skills / OpenClaw 适配器
try:
    from .eastmoney_skills import EastmoneySkillsClient, get_eastmoney_skills_client, eastmoney_skills_available
    EASTMONEY_SKILLS_AVAILABLE = True
except ImportError:
    EastmoneySkillsClient = None
    get_eastmoney_skills_client = None
    eastmoney_skills_available = None
    EASTMONEY_SKILLS_AVAILABLE = False

# 导入基本面快照工具
try:
    from .fundamentals_snapshot import get_fundamentals_snapshot
    FUNDAMENTALS_SNAPSHOT_AVAILABLE = True
except ImportError:
    get_fundamentals_snapshot = None
    FUNDAMENTALS_SNAPSHOT_AVAILABLE = False

__all__ = [
    'AKShareProvider',
    'AKSHARE_AVAILABLE',
    'TushareProvider',
    'TUSHARE_AVAILABLE',
    'BaostockProvider',
    'BAOSTOCK_AVAILABLE',
    'get_fundamentals_snapshot',
    'FUNDAMENTALS_SNAPSHOT_AVAILABLE',
    'EastmoneySkillsClient',
    'get_eastmoney_skills_client',
    'eastmoney_skills_available',
    'EASTMONEY_SKILLS_AVAILABLE',
]

