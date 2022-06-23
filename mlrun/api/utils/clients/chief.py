import copy

import fastapi
import requests.adapters
import urllib3

import mlrun.api.schemas
import mlrun.api.utils.projects.remotes.follower
import mlrun.errors
import mlrun.utils.singleton
from mlrun.utils import logger


# we were thinking to simply use httpdb, but decided to have a separated class for simplicity for now until
# this class evolves, but this should be reconsidered when adding more functionality to the class
class Client(
    metaclass=mlrun.utils.singleton.AbstractSingleton,
):
    """
    We chose chief-workers architecture to provide multi-instance API.
    By default, all API calls can access both the chief and workers.
    The key distinction is that some responsibilities, such as scheduling jobs, are exclusively performed by the chief.
    Instead of limiting the ui/client to only send requests to the chief, because the workers doesn't hold all the
    information. When one of the workers receives a request that the chief needs to execute or may have the knowledge
    of that piece of information, the worker will redirect the request to the chief.
    """

    def __init__(self) -> None:
        super().__init__()
        http_adapter = requests.adapters.HTTPAdapter(
            max_retries=urllib3.util.retry.Retry(total=3, backoff_factor=1),
            pool_maxsize=int(mlrun.mlconf.httpdb.max_workers),
        )
        self._session = requests.Session()
        self._session.mount("http://", http_adapter)
        self._api_url = mlrun.mlconf.resolve_chief_api_url()
        # remove backslash from end of api url
        self._api_url = (
            self._api_url[:-1] if self._api_url.endswith("/") else self._api_url
        )

    def get_internal_background_task(
        self, name: str, request: fastapi.Request = None
    ) -> fastapi.Response:
        return self._proxy_request_to_chief("GET", f"background-tasks/{name}", request)

    def trigger_migrations(self, request: fastapi.Request = None) -> fastapi.Response:
        return self._proxy_request_to_chief("POST", "operations/migrations", request)

    def create_schedule(
        self, project: str, request: fastapi.Request, body: dict
    ) -> fastapi.Response:
        return self._proxy_request_to_chief(
            "POST", f"projects/{project}/schedules", request, body
        )

    def update_schedule(
        self, project: str, name: str, request: fastapi.Request, body: dict
    ) -> fastapi.Response:
        return self._proxy_request_to_chief(
            "PUT", f"projects/{project}/schedules/{name}", request, body
        )

    def delete_schedule(
        self, project: str, name: str, request: fastapi.Request
    ) -> fastapi.Response:
        return self._proxy_request_to_chief(
            "DELETE", f"projects/{project}/schedules/{name}", request
        )

    def delete_schedules(
        self, project: str, request: fastapi.Request
    ) -> fastapi.Response:
        return self._proxy_request_to_chief(
            "DELETE", f"projects/{project}/schedules", request
        )

    def invoke_schedule(
        self, project: str, name: str, request: fastapi.Request
    ) -> fastapi.Response:
        return self._proxy_request_to_chief(
            "POST", f"projects/{project}/schedules/{name}/invoke", request
        )

    def submit_job(self, request: fastapi.Request, body: dict) -> fastapi.Response:
        return self._proxy_request_to_chief("POST", "submit_job", request, body)

    def build_function(self, request: fastapi.Request, body: dict) -> fastapi.Response:
        return self._proxy_request_to_chief("POST", "build/function", request, body)

    def delete_project(self, name, request: fastapi.Request) -> fastapi.Response:
        return self._proxy_request_to_chief("DELETE", f"projects/{name}", request)

    def _proxy_request_to_chief(
        self,
        method,
        path,
        request: fastapi.Request = None,
        body: dict = None,
        raise_on_failure: bool = False,
    ) -> fastapi.Response:
        request_kwargs = self._resolve_request_kwargs_from_request(request, body)

        chief_response = self._send_request_to_api(
            method=method,
            path=path,
            raise_on_failure=raise_on_failure,
            **request_kwargs,
        )

        return self._convert_requests_response_to_fastapi_response(chief_response)

    @staticmethod
    def _resolve_request_kwargs_from_request(
        request: fastapi.Request = None, body: dict = None
    ) -> dict:
        kwargs = {}
        if request:
            data = body if body else {}
            kwargs.update({"data": data})
            kwargs.update({"headers": dict(request.headers)})
            kwargs.update({"params": dict(request.query_params)})
            kwargs.update({"cookies": request.cookies})
        return kwargs

    @staticmethod
    def _convert_requests_response_to_fastapi_response(
        chief_response: requests.Response,
    ) -> fastapi.Response:
        # based on the way we implemented the exception handling for endpoints in MLRun we can expect the media type
        # of the response to be of type application/json, see mlrun.api.http_status_error_handler for reference
        return fastapi.responses.Response(
            content=chief_response.content,
            status_code=chief_response.status_code,
            headers=dict(
                chief_response.headers
            ),  # chief_response.headers is of type CaseInsensitiveDict
            media_type="application/json",
        )

    # TODO change this to use async calls
    def _send_request_to_api(
        self, method, path, raise_on_failure: bool = False, **kwargs
    ):
        url = f"{self._api_url}/api/{mlrun.mlconf.api_base_version}/{path}"
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = 20
        logger.debug("Sending request to chief", method=method, url=url, **kwargs)
        response = self._session.request(method, url, verify=False, **kwargs)
        if not response.ok:
            log_kwargs = copy.deepcopy(kwargs)
            log_kwargs.update({"method": method, "path": path})
            if response.content:
                try:
                    data = response.json()
                    error = data.get("error")
                    error_stack_trace = data.get("errorStackTrace")
                except Exception:
                    pass
                else:
                    log_kwargs.update(
                        {"error": error, "error_stack_trace": error_stack_trace}
                    )
            logger.warning("Request to chief failed", **log_kwargs)
            if raise_on_failure:
                mlrun.errors.raise_for_status(response)
            return response
        # there are some responses like NO-CONTENT which doesn't return a json body
        try:
            data = response.json()
        except Exception:
            data = response.text
        logger.debug(
            "Request to chief succeeded",
            method=method,
            url=url,
            **kwargs,
            response=data,
        )
        return response