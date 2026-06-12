<?php

declare(strict_types=1);

/**
 * SPDX-FileCopyrightText: 2024 Nextcloud GmbH and Nextcloud contributors
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

namespace OCA\AppAPI\Service;

use OCA\AppAPI\Db\ExApp;
use OCP\DB\QueryBuilder\IQueryBuilder;
use OCP\IDBConnection;
use Psr\Log\LoggerInterface;

class EventsListenerService {
	public function __construct(
		private readonly IDBConnection $db,
		private readonly AppAPIService $appAPIService,
		private readonly ExAppService  $exAppService,
		private readonly LoggerInterface $logger,
	) {
	}

	public function registerListener(string $appId, string $eventType, array $eventSubtypes, string $actionHandler): ?array {
		$existingListener = $this->getListener($appId, $eventType);
		if ($existingListener !== null) {
			$this->unregisterListener($appId, $eventType);
		}

		$qb = $this->db->getQueryBuilder();
		$qb->insert('ex_event_handlers')
			->values([
				'appid'          => $qb->createNamedParameter($appId),
				'event_type'     => $qb->createNamedParameter($eventType),
				'event_subtypes' => $qb->createNamedParameter(json_encode($eventSubtypes)),
				'action_handler' => $qb->createNamedParameter($actionHandler),
			]);

		try {
			$qb->executeStatement();
		} catch (\Exception $e) {
			$this->logger->error('Failed to register event listener', ['exception' => $e]);
			return null;
		}

		return [
			'appid'          => $appId,
			'event_type'     => $eventType,
			'event_subtypes' => $eventSubtypes,
			'action_handler' => $actionHandler,
		];
	}

	public function unregisterListener(string $appId, string $eventType): bool {
		$qb = $this->db->getQueryBuilder();
		$qb->delete('ex_event_handlers')
			->where($qb->expr()->eq('appid', $qb->createNamedParameter($appId)))
			->andWhere($qb->expr()->eq('event_type', $qb->createNamedParameter($eventType)));

		try {
			return $qb->executeStatement() > 0;
		} catch (\Exception $e) {
			$this->logger->error('Failed to unregister event listener', ['exception' => $e]);
			return false;
		}
	}

	public function getListener(string $appId, string $eventType): ?array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('*')
			->from('ex_event_handlers')
			->where($qb->expr()->eq('appid', $qb->createNamedParameter($appId)))
			->andWhere($qb->expr()->eq('event_type', $qb->createNamedParameter($eventType)));

		$result = $qb->executeQuery();
		$row = $result->fetch();
		$result->closeCursor();

		if ($row === false) {
			return null;
		}

		return [
			'appid'          => $row['appid'],
			'event_type'     => $row['event_type'],
			'event_subtypes' => json_decode($row['event_subtypes'], true),
			'action_handler' => $row['action_handler'],
		];
	}

	/**
	 * Get all registered listeners for a given eventType and subtype.
	 */
	public function getListenersForEvent(string $eventType, string $eventSubtype): array {
		$qb = $this->db->getQueryBuilder();
		$qb->select('*')
			->from('ex_event_handlers')
			->where($qb->expr()->eq('event_type', $qb->createNamedParameter($eventType)));

		$result = $qb->executeQuery();
		$rows = $result->fetchAll();
		$result->closeCursor();

		$listeners = [];
		foreach ($rows as $row) {
			$subtypes = json_decode($row['event_subtypes'], true) ?? [];
			if (in_array($eventSubtype, $subtypes, true)) {
				$listeners[] = [
					'appid'          => $row['appid'],
					'event_type'     => $row['event_type'],
					'event_subtypes' => $subtypes,
					'action_handler' => $row['action_handler'],
				];
			}
		}
		return $listeners;
	}

	/**
	 * Dispatch an event to all registered exApps listening for it.
	 */
	public function dispatchEvent(string $eventType, string $eventSubtype, array $eventData): void {
		$listeners = $this->getListenersForEvent($eventType, $eventSubtype);
		foreach ($listeners as $listener) {
			$exApp = $this->exAppService->getExApp($listener['appid']);
			if ($exApp === null || !$exApp->getEnabled()) {
				continue;
			}

			$payload = [
				'event_type'    => $eventType,
				'event_subtype' => $eventSubtype,
				'event_data'    => $eventData,
			];

			try {
				$this->appAPIService->requestToExApp(
					$exApp,
					$listener['action_handler'],
					null,
					'POST',
					$payload,
				);
			} catch (\Exception $e) {
				$this->logger->error(
					sprintf('Failed to dispatch %s event to %s', $eventSubtype, $listener['appid']),
					['exception' => $e],
				);
			}
		}
	}

	/**
	 * Remove all event listeners for a given exApp (called on unregister).
	 */
	public function unregisterAllForApp(string $appId): void {
		$qb = $this->db->getQueryBuilder();
		$qb->delete('ex_event_handlers')
			->where($qb->expr()->eq('appid', $qb->createNamedParameter($appId)));
		try {
			$qb->executeStatement();
		} catch (\Exception $e) {
			$this->logger->error('Failed to remove event listeners for ' . $appId, ['exception' => $e]);
		}
	}
}
