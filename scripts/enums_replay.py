# =====================================================================
# enums_replay.py
# =====================================================================
#
# АВТОМАТИЧНО ЗГЕНЕРОВАНО З replay/football-pro-sample
#
# ЦЕЙ ФАЙЛ МІСТИТЬ ЄДИНИЙ ФОРМАТ НАЗВ КЛЮЧІВ REPLAY-ФАЙЛУ
#
# Формат:
#   ENUM_NAME = 'rawKey'
#
# =====================================================================

# Верхній рівень JSON
class ROOT:
    CLK = 'clk'
    MC = 'mc'
    OP = 'op'
    PT = 'pt'

# Рівень mc[]
class MARKET_CHANGE:
    CON = 'con'
    ID = 'id'
    IMG = 'img'
    MARKET_DEFINITION = 'marketDefinition'
    RC = 'rc'
    TV = 'tv'

# Рівень marketDefinition
class MARKET_DEFINITION:
    BET_DELAY = 'betDelay'
    BETTING_TYPE = 'bettingType'
    BSP_MARKET = 'bspMarket'
    BSP_RECONCILED = 'bspReconciled'
    COMPLETE = 'complete'
    COUNTRY_CODE = 'countryCode'
    CROSS_MATCHING = 'crossMatching'
    DISCOUNT_ALLOWED = 'discountAllowed'
    EVENT_ID = 'eventId'
    EVENT_NAME = 'eventName'
    EVENT_TYPE_ID = 'eventTypeId'
    IN_PLAY = 'inPlay'
    MARKET_BASE_RATE = 'marketBaseRate'
    MARKET_TIME = 'marketTime'
    MARKET_TYPE = 'marketType'
    NAME = 'name'
    NUMBER_OF_ACTIVE_RUNNERS = 'numberOfActiveRunners'
    NUMBER_OF_WINNERS = 'numberOfWinners'
    OPEN_DATE = 'openDate'
    PERSISTENCE_ENABLED = 'persistenceEnabled'
    REGULATORS = 'regulators'
    RUNNERS = 'runners'
    RUNNERS_VOIDABLE = 'runnersVoidable'
    SETTLED_TIME = 'settledTime'
    STATUS = 'status'
    SUSPEND_TIME = 'suspendTime'
    TIMEZONE = 'timezone'
    TURN_IN_PLAY_ENABLED = 'turnInPlayEnabled'
    VERSION = 'version'

# Рівень marketDefinition.runners[]
class RUNNER:
    BSP = 'bsp'
    HC = 'hc'
    ID = 'id'
    NAME = 'name'
    SORT_PRIORITY = 'sortPriority'
    STATUS = 'status'

# Рівень rc[]
class RUNNER_CHANGE:
    ATB = 'atb'
    ATL = 'atl'
    HC = 'hc'
    ID = 'id'
    LTP = 'ltp'
    SPB = 'spb'
    SPF = 'spf'
    SPL = 'spl'
    SPN = 'spn'
    TRD = 'trd'
    TV = 'tv'
