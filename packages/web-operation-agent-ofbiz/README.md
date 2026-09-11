# web-operation-agent-ofbiz

Apache OFBiz support for [`web-operation-agent`](https://github.com/): the OFBiz `/webapp/control/request`
route grammar, the XML-descriptor source compiler, and the App Skill provider for pages the
current menu does not expose.

This package contains no generic Agent code. It exists only to implement the core package's
ports (`ApplicationRoutePort`, `SourceCompiler`, `ApplicationKnowledgePort`) against Apache
OFBiz's own declarative configuration.

```python
from web_operation_agent_ofbiz import OFBIZ_ROUTE_PORT, OFBizCompiler
```

See the core package's README for how a route port and a compiled `SourceMap` are used by a
session.
