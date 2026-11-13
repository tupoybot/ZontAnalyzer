import assert from 'node:assert/strict';

export async function checkOwnerExperiments(page, baseURL) {
  await page.goto(baseURL + '/owner-experiment.html', {waitUntil:'networkidle'});
  assert.equal(await page.locator('.recommendation').count(), 0, 'standalone action works without recommendation');
  const journal = page.locator('[data-experiment-journal]');
  const open = async () => {
    await journal.locator(':scope > details > summary').click();
    await journal.locator('[data-change="device_id"] option').first().waitFor({state:'attached'});
  };
  await open();
  await journal.locator('[name="state"]').selectOption('performed');
  await journal.locator('[name="goal"]').fill('Сохранить комфорт без лишних запусков');
  await journal.locator('[name="expected_effect"]').fill('Ровнее температура');
  await journal.locator('[name="observation_window"]').fill('7 суток');
  await journal.locator('[name="performed_at"]').fill('2026-08-01T12:30');
  await journal.locator('[data-change="parameter"]').fill('Кривая');
  await journal.locator('[data-change="before"]').fill('1');
  await journal.locator('[data-change="after"]').fill('1.2');
  await journal.locator('[data-add-change]').click();
  await journal.locator('[data-change="parameter"]').nth(1).fill('Предел');
  await journal.locator('[data-change="before"]').nth(1).fill('50');
  await journal.locator('[data-change="after"]').nth(1).fill('45');
  const api = '**/api/reports/*/experiments';
  await page.route(api, async route => {
    if (route.request().method() === 'PUT') await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Проверочная ошибка сохранения'})});
    else await route.continue();
  });
  await journal.locator('[type="submit"]').click();
  await journal.locator('[data-experiment-message]').filter({hasText:'Проверочная ошибка'}).waitFor();
  assert.equal(await journal.locator('[name="goal"]').inputValue(), 'Сохранить комфорт без лишних запусков');
  assert.equal(await journal.locator('[data-change="after"]').nth(1).inputValue(), '45');
  await page.unroute(api);
  await journal.locator('[type="submit"]').click();
  await journal.locator('[data-experiment-message]').filter({hasText:'Сохранено.'}).waitFor();
  await page.reload({waitUntil:'networkidle'});
  await open();
  const card = journal.locator('[data-experiment-list] article').first();
  await card.getByRole('button', {name:'Изменить / завершить / откатить'}).click();
  assert.equal(await journal.locator('[data-change="parameter"]').count(), 2);
  assert.match(await journal.locator('[name="performed_at"]').inputValue(), /^2026-08-01T12:30/);
  await journal.locator('[name="goal"]').fill('Уточнённая цель');
  await journal.locator('[name="state"]').selectOption('completed');
  await journal.locator('[type="submit"]').click();
  await journal.locator('[data-experiment-message]').filter({hasText:'Сохранено.'}).waitFor();
  await card.getByRole('button', {name:'Изменить / завершить / откатить'}).click();
  await journal.locator('[name="state"]').selectOption('rollback_planned');
  await journal.locator('[type="submit"]').click();
  await journal.locator('[data-experiment-list]').filter({hasText:'Откат запланирован'}).waitFor();
  await card.getByRole('button', {name:'Изменить / завершить / откатить'}).click();
  await journal.locator('[name="state"]').selectOption('rolled_back');
  await journal.locator('[name="rollback_at"]').fill('2026-08-03T18:15');
  await journal.locator('[type="submit"]').click();
  await journal.locator('[data-experiment-list]').filter({hasText:'Откат выполнен владельцем'}).waitFor();
  await card.locator('details > summary').click();
  assert.match(await card.locator('pre').textContent(), /Исходная цель: Сохранить комфорт без лишних запусков/);
  assert.match(await card.textContent(), /подтверждение телеметрией отсутствует/);
}
