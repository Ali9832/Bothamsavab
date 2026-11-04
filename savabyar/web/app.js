'use strict';
const webapp = window.Bale?.WebApp;
const startParams = new URLSearchParams(location.search);
let intent = startParams.get('intent') || startParams.get('tgWebAppStartParam') || webapp?.initDataUnsafe?.start_param;
const intentions = {
  mahdavi: ['مشارکت در مهدویت', 'مشارکت شما برای فعالیت‌ها و برنامه‌های مهدوی مصرف می‌شود.'],
  heyat: ['مشارکت در هیئت', 'مشارکت شما برای برنامه‌ها و هزینه‌های هیئت مصرف می‌شود.'],
  unrestricted: ['مشارکت بدون نیت مشخص', 'اجازه می‌دهید مبلغ مشارکت، بر اساس نیاز در مهدویت یا هیئت مصرف شود.'],
};
const amount = document.querySelector('#amount');
const pay = document.querySelector('#pay');
const status = document.querySelector('#status');
let requestId = null;
let bounds = null;
let busy = false;

function selectIntent(selected) {
  if (busy) return;
  const valid = Object.hasOwn(intentions, selected);
  intent = valid ? selected : null;
  document.querySelector('#intent-selection').hidden = valid;
  document.querySelector('#amount-card').hidden = !valid;
  if (valid) {
    document.querySelector('#intent').textContent = intentions[intent][0];
    document.querySelector('#purpose').textContent = intentions[intent][1];
    webapp?.BackButton?.show();
  } else {
    webapp?.BackButton?.hide();
  }
  amount.value = '';
  lastFormattedAmount = '';
  requestId = null;
  notify('');
}

document.querySelectorAll('[data-intent]').forEach(button => {
  button.addEventListener('click', () => selectIntent(button.dataset.intent));
});
document.querySelector('#change-intent').addEventListener('click', () => selectIntent(null));
webapp?.BackButton?.onClick(() => selectIntent(null));

function notify(text, error = false) {
  status.textContent = text;
  status.className = error ? 'error' : '';
}

async function post(path, data) {
  const response = await fetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({...data, init_data: webapp?.initData || ''}),
    signal: AbortSignal.timeout(20000),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'درخواست انجام نشد؛ دوباره تلاش کنید.');
  return result;
}

function normalizedAmount() {
  return amount.value.trim().replace(/[۰-۹]/g, c => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(c)))
    .replace(/[٠-٩]/g, c => String('٠١٢٣٤٥٦٧٨٩'.indexOf(c)));
}

async function confirmPayment(orderId) {
  notify('در حال دریافت تأیید پرداخت از بله…');
  for (let i = 0; i < 20; i++) {
    const result = await post('/api/status', {order_id: orderId});
    if (result.status === 'paid') {
      notify(`${result.is_test ? 'پرداخت آزمایشی تأیید شد' : 'مشارکت شما با موفقیت ثبت شد'} 🌿\nشماره رسید: ${result.receipt_id}`);
      return true;
    }
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
  notify('تأیید پرداخت هنوز دریافت نشده است. پس از تأیید بله، رسید در گفتگوی بات ارسال می‌شود.');
  return false;
}

document.querySelector('#payment-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  const [major = 0, minor = 0] = String(webapp?.version || '0.0').split('.').map(Number);
  if (!webapp?.initData || typeof webapp.openInvoice !== 'function'
      || (major < 1 || (major === 1 && minor < 3))) {
    notify('برای پرداخت، این صفحه را از دکمه‌های بات ثواب‌یار در نسخه به‌روز بله باز کنید.', true);
    return;
  }
  const value = normalizedAmount();
  const plain = value.replace(/[,٬]/g, '');
  if (!bounds || !/^(?:[0-9]+|[0-9]{1,3}(?:[,٬][0-9]{3})+)$/.test(value)
      || !Number.isSafeInteger(Number(plain)) || Number(plain) < bounds.minimum || Number(plain) > bounds.maximum) {
    notify('مبلغ را به صورت عدد صحیح و در محدوده مشخص‌شده وارد کنید.', true);
    return;
  }
  busy = true;
  pay.disabled = true;
  amount.disabled = true;
  try {
    requestId ||= crypto.randomUUID();
    notify('در حال آماده‌سازی پرداخت…');
    const invoice = await post('/api/invoice', {intent, amount_toman: value, request_id: requestId});
    if (invoice.status === 'paid') {
      await confirmPayment(invoice.order_id);
    } else {
      // Bale requires the createInvoiceLink result to be passed unchanged.
      const result = await new Promise((resolve, reject) => {
        try { webapp.openInvoice(invoice.invoice_params, resolve); } catch (error) { reject(error); }
      });
      if (result?.status === 'paid' || result?.status === 'pending') {
        await confirmPayment(invoice.order_id);
      } else {
        notify(result?.status === 'cancelled' ? 'پرداخت لغو شد؛ می‌توانید دوباره ادامه دهید.' : 'پرداخت تکمیل نشد؛ دوباره تلاش کنید.', true);
      }
    }
  } catch (error) {
    notify(error.name === 'TimeoutError' || error instanceof TypeError
      ? 'ارتباط برقرار نشد؛ دوباره تلاش کنید.' : error.message, true);
  } finally {
    busy = false;
    pay.disabled = false;
    amount.disabled = false;
  }
});
let lastFormattedAmount = '';
amount.addEventListener('input', () => {
  const cursor = amount.selectionStart ?? amount.value.length;
  const normalize = value => value.replace(/[۰-۹]/g, c => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(c)))
    .replace(/[٠-٩]/g, c => String('٠١٢٣٤٥٦٧٨٩'.indexOf(c)));
  const value = normalize(amount.value);
  if (!/^[0-9,٬\s]*$/.test(value)) {
    amount.value = lastFormattedAmount;
    notify('فقط عدد وارد کنید.', true);
    return;
  }
  const digitsBeforeCursor = normalize(amount.value.slice(0, cursor)).replace(/[^0-9]/g, '').length;
  const digits = value.replace(/[^0-9]/g, '');
  const formatted = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  amount.value = formatted;
  let nextCursor = 0;
  let digitCount = 0;
  while (nextCursor < formatted.length && digitCount < digitsBeforeCursor) {
    if (/[0-9]/.test(formatted[nextCursor])) digitCount++;
    nextCursor++;
  }
  amount.setSelectionRange(nextCursor, nextCursor);
  lastFormattedAmount = formatted;
  requestId = null;
  notify('');
});

async function initialize() {
  selectIntent(intent);
  try {
    const response = await fetch('/api/config');
    if (!response.ok) throw new Error();
    bounds = await response.json();
    pay.disabled = false;
    document.querySelector('#limits').textContent = `از ${bounds.minimum.toLocaleString('fa-IR')} تا ${bounds.maximum.toLocaleString('fa-IR')} تومان`;
    document.querySelector('#test-mode').hidden = !bounds.is_test;
    if (!webapp?.initData) notify('پیش‌نمایش صفحه؛ پرداخت فقط از داخل بات بله امکان‌پذیر است.');
  } catch (_) {
    pay.disabled = true;
    notify('دریافت اطلاعات پرداخت ممکن نشد؛ صفحه را دوباره باز کنید.', true);
  }
  webapp?.ready();
  webapp?.expand();
}
initialize();
