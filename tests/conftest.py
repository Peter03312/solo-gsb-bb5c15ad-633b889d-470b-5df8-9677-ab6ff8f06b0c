import os
import tempfile

# 必须在导入 app 之前设置数据库路径（测试使用独立临时库）
_fd, _path = tempfile.mkstemp(prefix="royalty-test-", suffix=".db")
os.close(_fd)
os.remove(_path)
os.environ["ROYALTY_DB_PATH"] = _path
