# web-operation-agent-twenty

Twenty CRM support for [`web-operation-agent`](https://github.com/): a delegated-OAuth
`ApplicationAdapter` for Twenty's companies/people/opportunities objects, plus the Business
Skills that load them.

This package contains no generic Agent code. It exists only to implement the core package's
`ApplicationAdapter` contract against Twenty's own REST API.

```python
from web_operation_agent_twenty import TwentyApplicationAdapter
```

See the core package's README and `examples/twenty/` for how the adapter is composed into a
running session.
