# Автозапуск Codex Cloud для bug issue

ZontAnalyzer использует GitHub Actions как небольшой мост между GitHub Issues и
subscription-backed Codex Cloud.

Официальная GitHub-интеграция Codex запускает обычную cloud-задачу из
`@codex ...` комментария в pull request. Поэтому workflow не вызывает OpenAI API
и не запускает `codex exec` на GitHub runner. Вместо этого он создаёт рабочую
ветку и draft PR, после чего оставляет в PR комментарий от владельца репозитория.

## Политика запуска

Автоматически workflow запускается только если одновременно выполняются условия:

- автор issue — `tupoybot`;
- у issue есть label `bug`;
- событие — создание issue уже с label `bug` либо добавление label `bug`.

Также поддерживается ручной запуск через **Actions → Dispatch my bug issues to
Codex Cloud → Run workflow**. В поле `issue_number` указывается номер уже
существующего issue. Ручной запуск использует ту же политику безопасности:
issue всё равно должен быть создан `tupoybot` и иметь label `bug`.

Это позволяет запускать Codex для старого бага без снятия/повторного добавления
label и без ручного создания PR.

Повторные события и ручные запуски не создают второй PR: в issue сохраняется
скрытый marker диспетчеризации, а workflow выполняется последовательно для
одного issue.

## Что создаётся

Для issue `#123`:

- ветка `codex/issue-123`;
- временный файл `.codex/dispatch/issue-123.md` со snapshot issue;
- draft PR в `main`;
- комментарий `@codex ...` в PR.

Codex получает PR как GitHub-контекст и работает в настроенном Codex Cloud
environment. В prompt явно указано удалить временный dispatch-файл перед
завершением, прогнать проверки, не merge-ить PR и не закрывать issue.

## Обязательный GitHub secret

Нужен repository Actions secret:

`CODEX_GITHUB_TOKEN`

Это должен быть **fine-grained personal access token пользователя `tupoybot`**
только для репозитория `tupoybot/ZontAnalyzer`.

Минимальные repository permissions:

- Contents: Read and write;
- Pull requests: Read and write;
- Issues: Read and write;
- Metadata: Read (выдаётся автоматически).

PAT нужен не для OpenAI. Он нужен, чтобы автоматический `@codex` комментарий
был опубликован от имени владельца с разрешением на запуск Codex, а не от
`github-actions[bot]`.

Добавить token:

1. GitHub repository → Settings → Secrets and variables → Actions.
2. New repository secret.
3. Name: `CODEX_GITHUB_TOKEN`.
4. Value: fine-grained PAT.

## Codex

Для репозитория должен быть настроен Codex Cloud environment. GitHub/Codex
integration также должна быть включена для репозитория, чтобы `@codex`
комментарии в PR могли запускать cloud chat.

Workflow: `.github/workflows/codex-bug-autofix.yml`.

## Проверка автоматического запуска

После merge workflow в default branch:

1. создать issue от `tupoybot`;
2. добавить label `bug` (или создать issue сразу с ним);
3. убедиться, что Actions run создал `codex/issue-N` и draft PR;
4. в PR должен появиться комментарий `@codex ...`;
5. Codex должен отреагировать на комментарий и создать cloud task;
6. после работы Codex draft PR остаётся на human review.

Issue другого автора с label `bug` не должен запускать workflow.

## Запуск для уже существующего бага

Если issue уже существовал до появления workflow:

1. убедиться, что автор — `tupoybot`;
2. убедиться, что label `bug` установлен;
3. открыть **Actions → Dispatch my bug issues to Codex Cloud**;
4. нажать **Run workflow**;
5. указать только номер issue, например `42`.

Снимать и заново ставить label `bug` не требуется. Draft PR вручную создавать
тоже не требуется.
