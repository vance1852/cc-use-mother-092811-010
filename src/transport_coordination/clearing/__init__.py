"""委托运输收入清算子系统。

模块边界：
- ``engine``  ：纯函数清算引擎，规则 + 运输事实 -> 零和结算分录；
- ``service`` ：事务、幂等、权限、版本草案、争议、关账与审计还原；
- ``api``     ：HTTP/JSON 路由；
- ``acceptance``：离线端到端验收。
"""

from .service import ClearingService

__all__ = ["ClearingService"]
