# Docs

Notes made while I was porting stuff from original bot.

## Required models

Minimal sqlalchemy modeling so that this bot can connect to original db. No attempt to port migrations. Just snapshot of this point in time.

```py

from app.balancer.models import BalanceAnswer
from app.ladder.models import Player, LadderSettings, LadderQueue

from app.balancer.balancer import role_names
from app.balancer.managers import BalanceResultManager, BalanceAnswerManager
from app.ladder.managers import MatchManager, PlayerManager

```