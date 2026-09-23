/* Configuração do app nativo.
 *
 * Uma chave pública do RevenueCat por loja: lá dentro, o app do iPhone e o do
 * Android são dois apps diferentes, cada um com a sua. Elas são públicas de
 * propósito — o SDK roda no aparelho, então qualquer um consegue lê-las. Quem
 * NÃO pode vir para cá é a secret key: ela vive só no servidor.
 *
 * Com a chave da plataforma vazia o app funciona normalmente; só a tela de
 * assinatura avisa que a compra ainda não está disponível.
 */
window.RC_CHAVES = {
  ios: 'appl_rvDzWnllpOOmfaHAKcbpDVYetps',
  android: 'goog_VckMEzIGFTMkTrkmKdxCaRIXPBs',
};
