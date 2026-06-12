<?php

declare(strict_types=1);

/**
 * SPDX-FileCopyrightText: 2024 Nextcloud GmbH and Nextcloud contributors
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

namespace OCA\AppAPI\Controller;

use OCA\AppAPI\AppInfo\Application;
use OCA\AppAPI\Attribute\AppAPIAuth;
use OCA\AppAPI\Service\EventsListenerService;
use OCP\AppFramework\Http;
use OCP\AppFramework\Http\Attribute\NoCSRFRequired;
use OCP\AppFramework\Http\Attribute\PublicPage;
use OCP\AppFramework\Http\DataResponse;
use OCP\AppFramework\OCSController;
use OCP\IRequest;

class EventsListenerController extends OCSController {
	public function __construct(
		IRequest                              $request,
		private readonly EventsListenerService $eventsListenerService,
	) {
		parent::__construct(Application::APP_ID, $request);
	}

	#[NoCSRFRequired]
	#[PublicPage]
	#[AppAPIAuth]
	public function registerListener(
		string $eventType,
		array  $eventSubtypes,
		string $actionHandler,
	): DataResponse {
		$appId = $this->request->getHeader('EX-APP-ID');
		$result = $this->eventsListenerService->registerListener($appId, $eventType, $eventSubtypes, $actionHandler);

		if ($result === null) {
			return new DataResponse([], Http::STATUS_BAD_REQUEST);
		}

		return new DataResponse($result);
	}

	#[NoCSRFRequired]
	#[PublicPage]
	#[AppAPIAuth]
	public function unregisterListener(string $eventType): DataResponse {
		$appId = $this->request->getHeader('EX-APP-ID');
		$unregistered = $this->eventsListenerService->unregisterListener($appId, $eventType);

		if (!$unregistered) {
			return new DataResponse([], Http::STATUS_NOT_FOUND);
		}

		return new DataResponse();
	}

	#[NoCSRFRequired]
	#[PublicPage]
	#[AppAPIAuth]
	public function getListener(string $eventType): DataResponse {
		$appId = $this->request->getHeader('EX-APP-ID');
		$result = $this->eventsListenerService->getListener($appId, $eventType);

		if ($result === null) {
			return new DataResponse([], Http::STATUS_NOT_FOUND);
		}

		return new DataResponse($result);
	}
}
