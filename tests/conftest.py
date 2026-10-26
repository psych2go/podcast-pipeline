import os

# Never let the test suite spend the real pi refresh token.
os.environ.setdefault("SUBAGENT_PI_AUTH_LOCK", "off")
