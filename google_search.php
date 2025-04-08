<?php
function buscarGoogle($terminoBusqueda) {
    $apiKey = 'AIzaSyBn_GXjQfgf9CYDb5ji9iVI7-q9XyiMNPY';
    $cx = '0650d571365cd4765';

    $query = urlencode($terminoBusqueda);
    $url = "https://www.googleapis.com/customsearch/v1?key=$apiKey&cx=$cx&q=$query";

    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    $resultado = curl_exec($ch);
    curl_close($ch);

    return json_decode($resultado, true);
}

// Ejemplo rápido para probar ahora mismo claramente:
$resultados = buscarGoogle('Ashotel eventos Tenerife');

if (!empty($resultados['items'])) {
    foreach ($resultados['items'] as $item) {
        echo "<a href='{$item['link']}' target='_blank'>{$item['title']}</a><br>";
        echo "<p>{$item['snippet']}</p><hr>";
    }
} else {
    echo "No se encontraron resultados.";
}
?>
