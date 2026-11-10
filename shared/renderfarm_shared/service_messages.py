"""Compatibility translations for messages returned by older farm services."""
# Legacy message -> (English, Portuguese, Spanish). Protocol payloads stay unchanged.
SERVICE_MESSAGES = {
    "Worker nao encontrado.": ("Worker not found.", "Worker não encontrado.", "Worker no encontrado."),
    "Render device invalido. Use CPU ou GPU.": ("Invalid render device. Use CPU or GPU.", "Dispositivo inválido. Use CPU ou GPU.", "Dispositivo no válido. Usa CPU o GPU."),
    "Selecione pelo menos um worker.": ("Select at least one worker.", "Selecione pelo menos um worker.", "Selecciona al menos un worker."),
    "Nenhum worker selecionado esta online.": ("No selected worker is online.", "Nenhum worker selecionado está online.", "Ningún worker seleccionado está conectado."),
    "Nenhum worker selecionado conseguiu validar os caminhos.": ("No selected worker could validate the paths.", "Nenhum worker selecionado conseguiu validar os caminhos.", "Ningún worker seleccionado pudo validar las rutas."),
    "Nao ha job atual para salvar.": ("There is no current job to save.", "Não há job atual para salvar.", "No hay un trabajo actual para guardar."),
    "O arquivo .blend nao existe.": ("The .blend file does not exist.", "O arquivo .blend não existe.", "El archivo .blend no existe."),
    "Selecione um arquivo com extensao .blend.": ("Select a file with the .blend extension.", "Selecione um arquivo com extensão .blend.", "Selecciona un archivo con extensión .blend."),
    "O frame inicial precisa ser menor ou igual ao frame final.": ("The start frame must be less than or equal to the end frame.", "O frame inicial deve ser menor ou igual ao frame final.", "El frame inicial debe ser menor o igual que el final."),
    "O tamanho do bloco precisa ser maior que zero.": ("Block size must be greater than zero.", "O tamanho do bloco deve ser maior que zero.", "El tamaño del bloque debe ser mayor que cero."),
    "A pasta de saida informada nao e um diretorio valido.": ("The output folder is not a valid directory.", "A pasta de saída não é um diretório válido.", "La carpeta de salida no es un directorio válido."),
    "Nao ha job ativo ou selecionado para pausar.": ("Select a job to pause.", "Selecione um job para pausar.", "Selecciona un trabajo para pausar."),
    "Nao ha job ativo ou selecionado para retomar.": ("Select a job to resume.", "Selecione um job para retomar.", "Selecciona un trabajo para reanudar."),
    "Nao ha job ativo ou selecionado para cancelar.": ("Select a job to cancel.", "Selecione um job para cancelar.", "Selecciona un trabajo para cancelar."),
    "Nenhum Coordinator ativo foi encontrado. O Master continua em modo discovery na LAN.": ("No active Coordinator was found. The Master is still searching the local network.", "Nenhum Coordinator ativo foi encontrado. O Master continua procurando na rede local.", "No se encontró ningún Coordinator activo. El Master sigue buscando en la red local."),
    "Conectado ao Coordinator.": ("Connected to the Coordinator.", "Conectado ao Coordinator.", "Conectado al Coordinator."),
}

MESSAGE_PATTERNS = {
    "Conectado ao Coordinator em {url}.": ("Connected to the Coordinator at {url}.", "Conectado ao Coordinator em {url}.", "Conectado al Coordinator en {url}."),
    "Coordinator indisponivel: {error}": ("Coordinator unavailable: {error}", "Coordinator indisponível: {error}", "Coordinator no disponible: {error}"),
}
