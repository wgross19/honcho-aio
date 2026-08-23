# Suite Components

`honcho-aio` is a single image. It does not publish extra components.

Postgres, Redis, honcho-api, and honcho-deriver all run inside `dub19/honcho-aio` under s6. There is no separate agent image and no second XML template.
