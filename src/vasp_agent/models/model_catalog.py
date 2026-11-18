"""Discover exact model IDs without making a generation request."""


def list_models(client):
    try:
        models = sorted({item.id for item in client.models.list()
                         if isinstance(item.id, str) and item.id.strip()})
    except Exception as exc:
        status = getattr(exc, 'status_code', None)
        if status == 401:
            raise ValueError('模型列表鉴权失败（401）：请检查所选接口的密钥，不是模型名错误。') from None
        if status == 403:
            raise ValueError('接口拒绝模型列表访问（403）：请检查权限或服务商访问限制。') from None
        if status in (404, 405, 501):
            raise ValueError('此地址未提供兼容的 /models 列表接口；检查 base_url，或按服务商文档用 --model 手动指定。') from None
        if status == 429:
            raise ValueError('模型列表请求受限（429）：请检查限流或账户额度。') from None
        raise ValueError('无法读取模型列表：请检查网络、接口地址及服务商兼容性。没有切换接口。') from None
    if not models:
        raise ValueError('接口返回空模型列表；请核对账号权限或咨询服务商。')
    return models


def display_models(models, output=print):
    output('接口返回的模型（是否支持当前协议和工具调用尚未验证）：')
    for index, model in enumerate(models, 1):
        # Escape control characters supplied by remote endpoints.
        label = ''.join(c if c.isprintable() else repr(c)[1:-1] for c in model)
        output(f'{index:>3}. {label}')


def select_model(models, reader=input, output=print):
    display_models(models, output)
    while True:
        answer = reader('输入模型编号或完整名称（q 取消）：').strip()
        if answer.lower() in ('q', 'quit', '/exit'):
            return None
        if answer in models:
            return answer
        if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(models):
            return models[int(answer) - 1]
        output('选择无效，请输入列表中的编号或完整模型名称。')
