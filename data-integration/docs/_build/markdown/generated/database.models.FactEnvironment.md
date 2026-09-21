# database.models.FactEnvironment

### *class* database.models.FactEnvironment(\*\*kwargs)

Bases: `Base`

Environmental data (temperature, rainfall, etc.) stored in long format

#### \_\_init_\_(\*\*kwargs)

A simple constructor that allows initialization from kwargs.

Sets attributes on the constructed instance using the names and
values in `kwargs`.

Only keys that are present as
attributes of the instance’s class are allowed. These could be,
for example, any mapped columns or relationships.

### Methods

| [`__init__`](#id0)(\*\*kwargs)   | A simple constructor that allows initialization from kwargs.   |
|----------------------------------|----------------------------------------------------------------|

### Attributes

| [`date_id`](#database.models.FactEnvironment.date_id)         |    |
|---------------------------------------------------------------|----|
| [`fact_env_id`](#database.models.FactEnvironment.fact_env_id) |    |
| [`location`](#database.models.FactEnvironment.location)       |    |
| [`location_id`](#database.models.FactEnvironment.location_id) |    |
| `metadata`                                                    |    |
| `registry`                                                    |    |
| [`source`](#database.models.FactEnvironment.source)           |    |
| [`source_code`](#database.models.FactEnvironment.source_code) |    |
| [`time`](#database.models.FactEnvironment.time)               |    |
| [`value`](#database.models.FactEnvironment.value)             |    |
| [`variable`](#database.models.FactEnvironment.variable)       |    |

#### fact_env_id

#### location_id

#### date_id

#### variable

#### value

#### source_code

#### location

#### time

#### source

#### \_\_init_\_(\*\*kwargs)

A simple constructor that allows initialization from kwargs.

Sets attributes on the constructed instance using the names and
values in `kwargs`.

Only keys that are present as
attributes of the instance’s class are allowed. These could be,
for example, any mapped columns or relationships.
