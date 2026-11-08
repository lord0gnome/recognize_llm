<?php

declare(strict_types=1);

namespace OCA\RecognizeLlmSettings\Controller;

use OCA\RecognizeLlmSettings\AppInfo\Application;
use OCA\RecognizeLlmSettings\Service\DescriptionService;
use OCP\AppFramework\Http;
use OCP\AppFramework\Http\Attribute\ApiRoute;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\DataResponse;
use OCP\AppFramework\OCSController;
use OCP\IRequest;

/**
 * Read-only access to the description Recognize LLM generated for a file.
 */
class DescriptionController extends OCSController {
	public function __construct(
		IRequest $request,
		private DescriptionService $service,
		private ?string $userId,
	) {
		parent::__construct(Application::APP_ID, $request);
	}

	/**
	 * @return DataResponse<Http::STATUS_OK, array{description: ?string}, array{}>|DataResponse<Http::STATUS_NOT_FOUND|Http::STATUS_UNAUTHORIZED, array{description: null}, array{}>
	 */
	#[NoAdminRequired]
	#[ApiRoute(verb: 'GET', url: '/api/v1/description/{fileId}', requirements: ['fileId' => '\d+'])]
	public function get(int $fileId): DataResponse {
		if ($this->userId === null) {
			return new DataResponse(['description' => null], Http::STATUS_UNAUTHORIZED);
		}
		$found = $this->service->forUser($this->userId, $fileId);
		if ($found === false) {
			// Unknown file, or not visible to this user: same answer, so ids can't be probed.
			return new DataResponse(['description' => null], Http::STATUS_NOT_FOUND);
		}
		return new DataResponse(['description' => $found]);
	}
}
