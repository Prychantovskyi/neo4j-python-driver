# Neo4j Driver Change Log (breaking/major changes only)

## Version 5.0

- Python 3.10 support added
- Python 3.6 support has been dropped.
- `Result`, `Session`, and `Transaction` can no longer be imported from
  `neo4j.work`. They should've been imported from `neo4j` all along.  
  Remark: It's recommended to import everything needed directly from `noe4j` if
  available, not its submodules or subpackages.
- Experimental pipelines feature has been removed.
- Experimental async driver has been added.
- `ResultSummary.server.version_info` has been removed.  
  Use `ResultSummary.server.agent`, `ResultSummary.server.protocol_version`,
  or call the `dbms.components` procedure instead.
- SSL configuration options have been changed:
  - `trust` has been deprecated and will be removed in a future release.  
    Use `trusted_certificates` instead.
    See the API documentation for more details.
- `neo4j.time` module:
  - `Duration`
    - The constructor does not accept `subseconds` anymore.  
      Use `milliseconds`, `microseconds`, or `nanoseconds` instead.
    - The property `subseconds` has been removed.  
      Use `nanoseconds` instead.
    - The property `hours_minutes_seconds` has been removed.  
      Use `hours_minutes_seconds_nanoseconds` instead.
    - For all math operations holds: they are element-wise on
      (`months`, `days`, `nanoseconds`).
      This affects (i.e., changes) the working of `//`, `%`, `/`, and `*`.
      - Years are equal to 12 months.
      - Weeks are equal to 7 days.
      - `seconds`, `milliseconds`, `microseconds`, and `nanoseconds` are
        implicitly converted to `nanoseconds` or `seconds` as fit.
    - Multiplication and division allow for floats but will always result in
      integer values (round to nearest even).
  - `Time`
    - The constructor does not accept `float`s for `second` anymore.  
      Use `nanosecond` instead.
    - Ticks are now nanoseconds since midnight (`int`).
      - The property `ticks_ns` has been renamed to `ticks`.  
        The old `ticks` is no longer supported.
      - The property`from_ticks_ns` has been renamed to `from_ticks`.  
        The old `from_ticks` is no longer supported.
    - The property `second` returns an `int` instead of a `float`.  
      Use `nanosecond` to get the sub-second information.
    - The property `hour_minute_second` has been removed.  
      Use `hour_minute_second_nanosecond` instead.
  - `DateTime`
    - The property `hour_minute_second` has been removed.  
      Use `hour_minute_second_nanosecond` instead.
    - The property `second` returns an `int` instead of a `float`.  
      Use `nanosecond` to get the sub-second information.
- Creation of a driver with `bolt[+s[sc]]://` scheme and a routing context has
  been deprecated and will raise an error in the Future. The routing context was
  and will be silently ignored until then.
- Bookmarks
  - `Session.last_bookmark` was deprecated. Its behaviour is partially incorrect
    and cannot be fixed without breaking its signature.  
    Use `Session.last_bookmarks` instead.
  - `neo4j.Bookmark` was deprecated.  
    Use `neo4j.Bookmarks` instead.
- Deprecated closing of driver and session objects in their destructor.
  This behaviour is non-deterministic as there is no guarantee that the
  destructor will ever be called. A `ResourceWarning` is emitted instead.  
  Make sure to configure Python to output those warnings when developing your
  application locally (it does not by default).
- Result scope:  
  - Records of Results cannot be accessed (`peek`, `single`, `iter`, ...)
    after their owning transaction has been closed, committed, or rolled back.
    Previously, this would yield undefined behavior.
    It now raises a `ResultConsumedError`.
  - Records of Results cannot be accessed (`peek`, `single`, `iter`, ...)
    after the Result has been consumed (`Result.consume()`).
    Previously, this would always yield no records.
    It now raises a `ResultConsumedError`.
  - New method `Result.closed()` can be used to check for this condition if
    necessary.
- The undocumented return value of `driver.verify_connectivity()` has been
  removed. If you need information about the remote server, use
  `driver.get_server_info()` instead.
- Transaction functions (a.k.a. managed transactions):  
  The first argument of transaction functions is now a `ManagedTransaction`
  object. It behaves exactly like a regular `Transaction` object, except it
  does not offer the `commit`, `rollback`, `close`, and `closed` methods.  
  Those methods would have caused a hard to interpreted error previously. Hence,
  they have been removed.
- Deprecated Nodes' and Relationships' `id` property (`int`) in favor of
  `element_id` (`str`).  
  This also affects `Graph` objects as `graph.nodes[...]` and
 `graph.relationships[...]` now prefers strings over integers.
- `ServerInfo.connection_id` has been deprecated and will be removed in a
  future release. There is no replacement as this is considered internal
  information.
- Output of logging helper `neo4j.debug.watch` changes
  - ANSI colour codes for log output are now opt-in
  - Prepend log format with log-level (if colours are disabled)
  - Prepend log format with thread name and id
- Deprecated `neo4j.exceptions.Neo4jError.is_retriable()`.  
  Use `neo4j.exceptions.Neo4jError.is_retryable()` instead.
- Importing submodules from `neo4j.time` (`neo4j.time.xyz`) has been deprecated.
  Everything needed should be imported from `neo4j.time` directly.
- `neo4j.spatial.hydrate_point` and `neo4j.spatial.dehydrate_point` have been
  deprecated without replacement. They are internal functions.
- Importing `neo4j.packstream` has been deprecated. It's internal and should not
  be used by client code.
- Importing `neo4j.routing` has been deprecated. It's internal and should not
  be used by client code.
- Importing `neo4j.config` has been deprecated. It's internal and should not
  be used by client code.
- `neoj4.Config`, `neoj4.PoolConfig`, `neoj4.SessionConfig`, and
  `neoj4.WorkspaceConfig` have been deprecated without replacement. They are
  internal classes.
- Importing `neo4j.meta` has been deprecated. It's internal and should not
  be used by client code. `ExperimantalWarning` should be imported directly from
  `neo4j`. `neo4j.meta.version` is exposed through `neo4j.__vesrion__`
- Importing `neo4j.data` has been deprecated. It's internal and should not
  be used by client code. `Record` should be imported directly from `neo4j`
  instead. `neo4j.data.DataHydrator` and `neo4j.data.DataDeydrator` have been
  removed without replacement.


## Version 4.4

- Python 3.5 support has been dropped.


## Version 4.3

- Python 3.9 support added


## Version 4.2

- No driver changes have been made for Neo4j 4.2


## Version 4.1

- Routing context is now forwarded to the server for when required by server-side routing


## Version 4.0 - Breaking Changes

- The package version has jumped from `1.7` directly to `4.0`, in order to bring the version in line with Neo4j itself.
- The package can now no longer be installed as `neo4j-driver`; use `pip install neo4j` instead.
- The `neo4j.v1` subpackage is now no longer available; all imports should be taken from the `neo4j` package instead.
- Changed `session(access_mode)` from a positional to a keyword argument
- The `bolt+routing` scheme is now named `neo4j`
- Connections are now unencrypted by default; to reproduce former behaviour, add `encrypted=True` to Driver configuration
- Removed `transaction.success` flag usage pattern.

+ Python 3.8 supported.
+ Python 3.7 supported.
+ Python 3.6 supported.
+ Python 3.5 supported.
+ Python 3.4 support has been dropped.
+ Python 3.3 support has been dropped.
+ Python 3.2 support has been dropped.
+ Python 3.1 support has been dropped.
+ Python 3.0 support has been dropped.
+ Python 2.7 support has been dropped.
