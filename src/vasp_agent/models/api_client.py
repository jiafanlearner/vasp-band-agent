"""Shared transport settings for interactive requests and diagnostic probes."""


def _strict_ssl_context(client_class):
    """Use the installed SDK's HTTP stack and preserve its CA environment."""
    import importlib
    import ssl

    package = next((base.__module__.split('.')[0] for base in client_class.__mro__
                    if base.__module__.split('.')[0] in ('httpx', 'httpx2')), None)
    if package is None:
        raise ValueError('无法识别 SDK 的 HTTP 客户端；未创建 direct 连接。')
    http = importlib.import_module(package)
    factory = getattr(http, 'create_ssl_context', None)
    if factory is None:
        factory = importlib.import_module(package + '._config').create_ssl_context
    # trust_env applies only to CA loading here. The client below separately
    # disables application proxy discovery with trust_env=False.
    context = factory(verify=True, trust_env=True)
    if (not isinstance(context, ssl.SSLContext) or
            context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname):
        raise ValueError('direct 连接必须验证证书及主机名。')
    return context


def create_client(config, timeout=180):
    from openai import DefaultHttpxClient, OpenAI

    mode = getattr(config, 'network_mode', 'auto')
    if mode not in ('auto', 'direct'):
        raise ValueError('network_mode 必须为 auto 或 direct。')
    # Use standard-library decoders. Some installations combine a newer HTTP
    # client with an older optional Brotli decoder that cannot accept its API.
    # Content negotiation changes neither TLS verification nor the API route.
    options = dict(api_key=config.api_key, base_url=config.base_url, timeout=timeout,
                   max_retries=0, default_headers={'Accept-Encoding': 'gzip, deflate'})
    if mode == 'auto':
        return OpenAI(**options)
    http_client = DefaultHttpxClient(trust_env=False, http2=False, timeout=timeout,
                                    verify=_strict_ssl_context(DefaultHttpxClient))
    try:
        return OpenAI(**options, http_client=http_client)
    except BaseException:
        http_client.close()
        raise
