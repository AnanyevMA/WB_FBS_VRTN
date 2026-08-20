/**
 * WB FBS Manager — Global State & Reference Tables
 */

const API_BASE = '/api/v1';

let currentSellerId = null;
let refreshInterval = null;
let currentSellersList = [];

// Authentication state stored in localStorage
let authToken = localStorage.getItem('wbfbs_auth_token') || '';
let currentUser = JSON.parse(localStorage.getItem('wbfbs_current_user') || 'null');

// CryptoPro signing & plugin state
let currentSigningPayload = null;
let cryptoProCerts = [];
let isCryptoProAvailable = false;

// Status dictionaries for localization & badge styling
const STATUS_MAP_ORDER = {
    'NEW': 'Новый',
    'ASSEMBLING': 'На сборке',
    'ASSEMBLED': 'Собран',
    'DELIVERING': 'В доставке',
    'DELIVERED': 'Доставлен',
    'CANCELLED': 'Отменен',
    'SORTED': 'Отсортирован'
};

const STATUS_MAP_KIZ = {
    'PENDING': 'Ожидает КИЗ',
    'ATTACHED': 'Прикреплен',
    'VALIDATED': 'Проверен',
    'WITHDRAWN': 'Выведен из оборота',
    'RETURNED': 'Возвращен',
    'ERROR': 'Ошибка КИЗ',
    'NOT_REQUIRED': 'Не требуется'
};

const STATUS_MAP_SUPPLY = {
    'CREATED': 'Создана',
    'CLOSED': 'Закрыта',
    'DELIVERING': 'В пути',
    'DELIVERED': 'Принята',
    'DONE': 'Завершена',
    'CANCELLED': 'Отменена'
};

const STATUS_MAP_CZ = {
    'INTRODUCED': 'В обороте',
    'IN_CIRCULATION': 'В обороте',
    'RETIRED': 'Выведен',
    'OUT_OF_CIRCULATION': 'Выведен',
    'EMITTED': 'Эмитирован',
    'EMISSION': 'Эмитирован',
    'APPLIED': 'Нанесен',
    'DISAGGREGATED': 'Списан',
    'WRITTEN_OFF': 'Списан',
    'KILLED': 'Списан'
};

const STATUS_MAP_WB = {
    'waiting': 'Ожидает приемки СЦ',
    'sorted': 'Отсортирован на СЦ',
    'ready_for_pickup': 'В ПВЗ (готов к выдаче)',
    'sold': 'Выкуплен покупателем',
    'canceled': 'Отменен WB',
    'canceled_by_client': 'Отказ при получении',
    'declined_by_client': 'Отменен клиентом (1-й час)',
    'defect': 'Брак'
};
