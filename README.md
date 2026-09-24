# 社区需求补短板决策

这是一个面向“一刻钟便民生活圈”运营协作的服务端项目。服务把领域记录、负责人和当前状态保存到本地 SQLite，应用层通过进程内请求适配器提供健康检查和基础登记能力，业务流程在这个边界上运行。

## 目录

- `src/community_needs/domain.py` 定义记录对象和时间处理。
- `src/community_needs/store.py` 负责 SQLite 连接、表结构和记录读写。
- `src/community_needs/service.py` 提供应用服务入口。
- `src/community_needs/api.py` 将 JSON 请求转换为服务调用。
- `tests/` 保存领域边界的可重复测试。

## 运行

运行测试：`PYTHONPATH=src python3 -m unittest discover -s tests`

检查源码：`python3 -m compileall src`

项目只使用 Python 标准库，测试和运行不需要启动其他服务。
