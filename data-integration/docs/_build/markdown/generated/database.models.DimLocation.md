# database.models.DimLocation

### *class* database.models.DimLocation(\*\*kwargs)

Bases: `Base`

Location hierarchy (country, region, district, site)

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

| [`admin_level`](#database.models.DimLocation.admin_level)     |    |
|---------------------------------------------------------------|----|
| [`centroid_lat`](#database.models.DimLocation.centroid_lat)   |    |
| [`centroid_lon`](#database.models.DimLocation.centroid_lon)   |    |
| [`concept_id`](#database.models.DimLocation.concept_id)       |    |
| [`country_code`](#database.models.DimLocation.country_code)   |    |
| [`country_name`](#database.models.DimLocation.country_name)   |    |
| [`location_code`](#database.models.DimLocation.location_code) |    |
| [`location_id`](#database.models.DimLocation.location_id)     |    |
| `metadata`                                                    |    |
| [`name`](#database.models.DimLocation.name)                   |    |
| `registry`                                                    |    |
| [`standard_code`](#database.models.DimLocation.standard_code) |    |

#### location_id

#### name

#### location_code

#### centroid_lat

#### centroid_lon

#### country_code

#### country_name

#### admin_level

#### standard_code

#### concept_id

#### \_\_init_\_(\*\*kwargs)

A simple constructor that allows initialization from kwargs.

Sets attributes on the constructed instance using the names and
values in `kwargs`.

Only keys that are present as
attributes of the instance’s class are allowed. These could be,
for example, any mapped columns or relationships.
