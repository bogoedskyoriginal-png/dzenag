# Analytics 1.0: контракт и совместимость

Источником требований служит полный активный промпт Analytics v3 канала «Перемотка».
Его текст и статус активности при обновлении не меняются. Схема реализована в
`dzen_agent/analytics_contract.py`; вход строится в `analytics_input.py`.

## Что изменилось относительно прежней схемы

Прежний результат содержал восемь полей: summary, best_topics, weak_topics,
useful_patterns, headline_patterns, recommendations, topics_to_avoid, opportunities.
В Analytics 1.0 обязательны все следующие разделы, включая вложенные поля:

| Раздел | Содержимое и проверка |
| --- | --- |
| contract_version, stage, status | Только 1.0, analytics и ok/partial/insufficient_data |
| period | start/end/as_of допускают null; ISO-даты, порядок дат и часовой пояс проверяются |
| data_quality | Число всех и пригодных статей, проблемы, отсутствующие метрики, ограничения сравнения |
| channel_diagnosis | Допустимые значения тренда и узкого места, summary |
| funnel | impressions/opens/completions/subscriptions и четыре отношения; неизвестные значения null |
| observations | ID, finding, metric, value/comparison_value, article_ids, три размера выборки, confidence, limitations |
| interpretations | ID, observation_ids, interpretation, alternative_explanations, confidence |
| cluster_signals | cluster, direction, article_count, evidence_ids, confidence, reason |
| experiments | ID, hypothesis, change, comparison, primary_metric, guardrail_metrics, evaluation_conditions, priority, evidence_ids |
| discovery_brief | promising_directions, underexplored_directions, avoid_repetition, research_questions |
| planner_brief | content_mix_guidance, priority_clusters, caution_clusters, headline_insights, format_insights, priority_experiment_ids |
| missing_data_requests | Список недостающих данных |

Неизвестные поля, неверные enum, отрицательные счётчики, NaN/Infinity, повторные ID
и ссылки на отсутствующие наблюдения/эксперименты отклоняются. Доли лежат в 0–1;
отношения проверяются по числителю и знаменателю. Публикации в наблюдениях должны
присутствовать во входе. Модель не может придумать период или подтверждённый тренд.

## Вход и метрики

Analytics получает один JSON с run_context, articles, channel_metrics,
metric_definitions, editorial_context, previous_analytics, experiment_history,
analysis_config. Поисковые инструменты Analytics не передаются.

Период измерения задаётся у импорта в разделе «Статистика». Дата импорта не выдаётся
за дату выгрузки. views не превращаются в opens; reads и subscribers сохраняют
исходные значения. CTR из CSV не считается автоматически долей.

Суммы и взвешенные отношения вычисляются только для уникальных публикаций с одинаковым
известным сопоставимым окном. Lifetime, повторные замеры, смешанные и неизвестные окна
не суммируются. Атрибуция подписок требует явного подтверждения. При более чем 200
строках передаётся равномерная выборка с предупреждением; суммы всего канала не выдумываются.
Временное сравнение требует соседнего проанализированного импорта, одинаковых определений
метрик, колонок и непересекающихся календарных периодов одинаковой длительности.

## Передача между этапами

Новые запуски фиксируют pipeline_flow=analytics_v1:

```text
Analytics → Discovery → Planner → утверждение плана → Research → Writer → Image → QA
```

Discovery получает discovery_brief и ищет кандидатов с проверяемыми источниками.
Planner получает planner_brief, результат Discovery и кандидатов; глубоких досье Research
на этом этапе ещё нет. После утверждения исследуются выбранные концепты, а Writer
получает ссылки уже из глубокого Research. При авторском Research 1.0 ручное утверждение
плана требуется также в AUTOPILOT: модель не должна получать вымышленное разрешение.

У Discovery отдельная модель (при пустом выборе используется Research model) и отдельный
промпт. Автоматического создания или замены промпта нет: согласованный текст нужно сохранить
в «Промпты → Discovery». Без него этап остановится с объяснением до платного вызова.

## Старые данные и восстановление

JSON без contract_version проверяется прежней схемой и сохраняется без перезаписи.
Для потребителей строится временная проекция двух brief из старых полей; она не выдаётся
за полноценную аналитику 1.0. Неизвестная версия контракта не трактуется как старый формат.
Запуски без pipeline_flow сохраняют прежний порядок Research → Planner.

Миграция SQLite 2 → 3 добавляет metadata_json импорта, признак superseded и таблицу
discovery_results, предварительно создавая резервную копию. Промпты, статьи, результаты
и расходы не удаляются. Миграция 3 → 4 добавляет исходный JSON Planner и visual_results
с источниками фотографий и результатом сборки. insufficient_data блокирует Discovery. До создания плана можно
явно подтвердить новый анализ исправленного импорта: прежний результат помечается
superseded, остаётся в истории, а новый вызов получает отдельный ключ оплаты.

Проверки контракта и сквозного сценария используют mock HTTP и не расходуют средства.
Качество реального ответа модели и живой адаптер публикации в Дзен сюда не входят.
