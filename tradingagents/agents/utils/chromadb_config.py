"""
ChromaDB 统一配置模块
支持 Windows 10/11 和其他操作系统的自动适配
"""
import os
import platform
import chromadb
from chromadb.config import Settings


def is_windows_11() -> bool:
    """
    检测是否为 Windows 11
    
    Returns:
        bool: 如果是 Windows 11 返回 True，否则返回 False
    """
    if platform.system() != "Windows":
        return False
    
    # Windows 11 的版本号通常是 10.0.22000 或更高
    version = platform.version()
    try:
        # 提取版本号，格式通常是 "10.0.26100"
        version_parts = version.split('.')
        if len(version_parts) >= 3:
            build_number = int(version_parts[2])
            # Windows 11 的构建号从 22000 开始
            return build_number >= 22000
    except (ValueError, IndexError):
        pass
    
    return False


def chroma_persist_enabled() -> bool:
    return os.getenv("MEMORY_CHROMA_PERSIST", "true").lower() == "true"


def get_chroma_persist_directory() -> str:
    raw = os.getenv("MEMORY_CHROMA_DIR") or os.path.join(os.getcwd(), "data", "chroma_memory")
    path = os.path.abspath(os.path.expanduser(raw))
    os.makedirs(path, exist_ok=True)
    return path


def get_persistent_chromadb_client():
    persist_directory = get_chroma_persist_directory()
    settings = Settings(
        allow_reset=True,
        anonymized_telemetry=False,
    )
    return chromadb.PersistentClient(path=persist_directory, settings=settings)


def get_transient_chromadb_client():
    settings = Settings(
        allow_reset=True,
        anonymized_telemetry=False,
        is_persistent=False,
    )
    return chromadb.Client(settings)


def get_win10_chromadb_client():
    """
    获取 Windows 10 兼容的 ChromaDB 客户端
    
    Returns:
        chromadb.Client: ChromaDB 客户端实例
    """
    if chroma_persist_enabled():
        return get_persistent_chromadb_client()

    try:
        return get_transient_chromadb_client()
    except Exception:
        basic_settings = Settings(
            allow_reset=True,
            anonymized_telemetry=False,
            is_persistent=False,
        )
        return chromadb.Client(basic_settings)


def get_win11_chromadb_client():
    """
    获取 Windows 11 优化的 ChromaDB 客户端
    
    Returns:
        chromadb.Client: ChromaDB 客户端实例
    """
    if chroma_persist_enabled():
        return get_persistent_chromadb_client()

    try:
        return get_transient_chromadb_client()
    except Exception:
        minimal_settings = Settings(
            allow_reset=True,
            anonymized_telemetry=False,
            is_persistent=False,
        )
        return chromadb.Client(minimal_settings)


def get_optimal_chromadb_client():
    """
    根据操作系统自动选择最优 ChromaDB 配置
    
    Returns:
        chromadb.Client: ChromaDB 客户端实例
    """
    system = platform.system()
    
    if system == "Windows":
        # 使用更准确的 Windows 11 检测
        if is_windows_11():
            # Windows 11 或更新版本
            return get_win11_chromadb_client()
        else:
            # Windows 10 或更老版本，使用兼容配置
            return get_win10_chromadb_client()
    else:
        if chroma_persist_enabled():
            return get_persistent_chromadb_client()
        return get_transient_chromadb_client()


# 导出配置
__all__ = [
    'chroma_persist_enabled',
    'get_chroma_persist_directory',
    'get_optimal_chromadb_client',
    'get_persistent_chromadb_client',
    'get_transient_chromadb_client',
    'get_win10_chromadb_client',
    'get_win11_chromadb_client',
    'is_windows_11'
]

