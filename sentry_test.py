from stockagent import observability
print("active:", observability.init_sentry(environment="local-test"))
observability.report(RuntimeError("wiring test, ignore"), stage="test")
observability.flush()
