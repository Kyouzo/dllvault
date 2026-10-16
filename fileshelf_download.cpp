// Fileshelf download integration.
// Dependencies: libcurl and nlohmann/json.

#include <cstdio>
#include <string>

#include <curl/curl.h>
#include <nlohmann/json.hpp>

namespace {

size_t WriteToString(void* contents, size_t size, size_t count, void* userData)
{
    const size_t bytes = size * count;
    static_cast<std::string*>(userData)->append(static_cast<char*>(contents), bytes);
    return bytes;
}

size_t WriteToFile(void* contents, size_t size, size_t count, void* userData)
{
    return std::fwrite(contents, size, count, static_cast<FILE*>(userData));
}

} // namespace

bool DownloadFileshelfFile(
    const std::string& fileshelfBaseUrl,
    const std::string& filename,
    const std::string& outputPath)
{
    CURL* curl = curl_easy_init();
    if (!curl)
        return false;

    char* escapedName = curl_easy_escape(curl, filename.c_str(), static_cast<int>(filename.size()));
    if (!escapedName)
    {
        curl_easy_cleanup(curl);
        return false;
    }

    std::string apiUrl = fileshelfBaseUrl;
    while (!apiUrl.empty() && apiUrl.back() == '/')
        apiUrl.pop_back();
    apiUrl += "/api/files/by-name/";
    apiUrl += escapedName;
    curl_free(escapedName);

    std::string response;
    curl_easy_setopt(curl, CURLOPT_URL, apiUrl.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteToString);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);
    curl_easy_setopt(curl, CURLOPT_FAILONERROR, 1L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "MyApp/1.0");

    CURLcode result = curl_easy_perform(curl);
    if (result != CURLE_OK)
    {
        curl_easy_cleanup(curl);
        return false;
    }

    std::string downloadUrl;
    try
    {
        const auto payload = nlohmann::json::parse(response);
        if (!payload.value("ok", false))
        {
            curl_easy_cleanup(curl);
            return false;
        }
        downloadUrl = payload.at("download_url").get<std::string>();
    }
    catch (const nlohmann::json::exception&)
    {
        curl_easy_cleanup(curl);
        return false;
    }

    FILE* file = nullptr;
#ifdef _WIN32
    if (fopen_s(&file, outputPath.c_str(), "wb") != 0)
        file = nullptr;
#else
    file = std::fopen(outputPath.c_str(), "wb");
#endif
    if (!file)
    {
        curl_easy_cleanup(curl);
        return false;
    }

    curl_easy_setopt(curl, CURLOPT_URL, downloadUrl.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, WriteToFile);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, file);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_FAILONERROR, 1L);
    result = curl_easy_perform(curl);

    std::fclose(file);
    curl_easy_cleanup(curl);
    return result == CURLE_OK;
}
