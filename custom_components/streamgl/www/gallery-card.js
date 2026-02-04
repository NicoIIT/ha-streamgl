import {
    LitElement,
    html,
    css
} from "https://unpkg.com/lit-element@2.0.1/lit-element.js?module";


class StreaMGLGalleryCard extends LitElement {
    static get properties() {
        return {
            _hass: {},
            config: {},
            resources: {},
            res_msg: {},
            currentResourceIndex: {},
            date_input: {},
            time_input: {},
            view_mode: {},
            update_sensor: {},
            gallery_last_update: {},
        };
    }

    render() {
        const menuAlignment = (this.config.menu_alignment || "bottom").toLowerCase();
        return html`
        ${this.errors == undefined ? html`` :
                this.errors.map((error) => {
                    return html`<hui-warning>${error}</hui-warning>`
                })}
        <ha-card .header=${this.config.title} class="menu-bottom">
          <div style="display:flex; flex-direction: row; justify-content: center; align-items: center">
            <div style="display:flex; flex-direction: row; justify-content: center; align-items: center">
              <button class="btn-date" @click="${ev => this._selectPrevDate()}"><ha-icon icon="mdi:chevron-double-left"/></button>
              <input class="date-input" id="dateinput" type="date" value="${this._getParsedDate()}" @change="${this._dateChanged}"/>
              <button class="btn-date" @click="${ev => this._selectNextDate()}"><ha-icon icon="mdi:chevron-double-right"/></button>
            </div>
            <button class="btn-reload" @click="${ev => this._reloadResources(this._hass)}"><ha-icon icon="mdi:reload"/></button>
          </div>
          <div class="resource-menu">
            ${this.res_msg.length !== 0 ?
                html`<div>${this.res_msg}</div>`
                : this.resources.map((resource, index) => {
                    return html`
                      <div id="resource${index}" data-imageIndex="${index}" @click="${ev => this._selectResource(index)}" class="resource-item${this._isResourceSelected(index) ? '-selected' : ''}">
                        <figure >
                          <img class="lzy_img" src="/streamgl/www/loading.jpg" data-src="${resource.urls.tnb}"/>
                        </figure>
                        <div class="title">${resource.caption} - ${resource.trigger}</div>
                      </div>
                    `;
                })}
          </div>
          ${this.currentResourceIndex != undefined ? html`<div class="resource-viewer" >
            <figure>
              ${this._isVideoMode() ?
                    html`<video controls src="${this._currentResourceUrl()}#t=0.1" @canplay="${ev => ev.target.play()}"/>`
                    : html`<img @click="${ev => this._toggleFullScreen(ev.target)}" src="${this._currentResourceUrl()}"/>`
                }
            </figure>
            <div class="title">${this._currentResourceFullTitle()}</div>
            <div class="button-bar">
            ${Object.entries({ 'clip': 'mdi:video', 'snap': 'mdi:image' }).map(([mode, icon]) => {
                    return (mode in this._currentResource().urls) ?
                        html`<button class="btn-mode${this.view_mode == mode ? '-selected' : ''}" @click="${ev => this._selectMode(mode)}"><ha-icon icon="${icon}"/></button>`
                        : html``
                })
                }
            <a class="btn-mode-selected" href="${this._currentResourceUrl()}" download="${this._currentResourceFilename()}"><ha-icon icon="mdi:download"/></a>
            </div>
          </div>` : ''}
        </ha-card>
    `;
    }

    updated(changedProperties) {
        const arr = this.shadowRoot.querySelectorAll('img.lzy_img')
        arr.forEach((v) => {
            this.imageObserver.observe(v);
        })
    }

    setConfig(config) {
        this.imageObserver = new IntersectionObserver((entries, imgObserver) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    const lazyImage = entry.target
                    lazyImage.src = lazyImage.dataset.src
                }
            })
        });

        this.config = config;

        this.update_sensor = (this.config.update_sensor === undefined) ? 'sensor.' + this.config.streamgl.replace(/-/g, "_") + '_gallery_size' : this.config.update_sensor
        this.gallery_last_update = ''

        this.view_mode = 'clip';

        window.addEventListener("keydown", (evt) => {
            if (this.currentResourceIndex === undefined) {
                return
            }
            switch (evt.code) {
                case "ArrowDown":
                case "ArrowRight":
                    this._selectNextResource();
                    break;
                case "ArrowUp":
                case "ArrowLeft":
                    this._selectPrevResource();
                    break;
                default:
                // null
            }
        });

        if (this._hass !== undefined)
            this._loadResources(this._hass);

    }

    set hass(hass) {
        this._hass = hass;
        let new_last_update = (this.update_sensor in hass.states) ? hass.states[this.update_sensor].last_updated : ""
        if (this.resources == null) {
            this._setupDateAndTime();
            this._loadResources(this._hass);
        }
        else if (this.gallery_last_update !== new_last_update) {
            this._loadResources(this._hass);
        }
        this.gallery_last_update = new_last_update
    }

    static getConfigElement() {
        return document.createElement("streamgl-gallery-ditor");
    }

    getCardSize() {
        return 1;
    }

    _setupDateAndTime() {
        const queryString = window.location.search;
        const urlParams = new URLSearchParams(queryString);
        if (urlParams.has('gallery_date')) {
            const date_str = urlParams.get('gallery_date')
            this.date_input = new Date("20" + date_str.substring(0, 2) + "-" + date_str.substring(2, 4) + "-" + date_str.substring(4, 6))
        }
        if (this.date_input == undefined) {
            this.date_input = new Date();
        }
        if (urlParams.has('gallery_time')) {
            const time_str = urlParams.get('gallery_time');
            this.time_input = time_str.substring(0, 2) + ":" + time_str.substring(2, 4) + ":" + time_str.substring(4, 6)
        }
    }

    _selectMode(iMode) {
        this.view_mode = iMode;
    }

    _getParsedDate() {
        return new Date(this.date_input.getTime() - (this.date_input.getTimezoneOffset() * 60 * 1000)).toISOString().split('T')[0]
    }

    _selectNextDate() {
        this.date_input.setDate(this.date_input.getDate() + 1)
        this.shadowRoot.getElementById('dateinput').value = this._getParsedDate();
        this._loadResources(this._hass);
    }

    _selectPrevDate() {
        this.date_input.setDate(this.date_input.getDate() - 1)
        this.shadowRoot.getElementById('dateinput').value = this._getParsedDate();
        this._loadResources(this._hass);
    }

    _toggleFullScreen(iTarget) {
        if (document.fullscreenElement !== null) {
            document.exitFullscreen();
        } else {
            iTarget.requestFullscreen();
        }
    }

    _selectPrevResource() {
        if (this.resources.length == 0) return;
        if (this.currentResourceIndex != 0) {
            this._selectResource(this.currentResourceIndex - 1);
        }
    }

    _selectNextResource() {
        if (this.resources.length == 0) return;
        if (this.currentResourceIndex != this.resources.length - 1) {
            this._selectResource(this.currentResourceIndex + 1);
        }
    }

    _selectResource(idx) {
        if (this.currentResourceIndex === idx) {
            this.currentResourceIndex = undefined
        } else {
            this.currentResourceIndex = idx;
            var elt = this.shadowRoot.querySelector("#resource" + this.currentResourceIndex);
            if (elt)
                elt.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
            this._select_best_view_mode()
        }
    }

    _select_best_view_mode() {
        let urls = this._currentResource().urls
        if (!(this.view_mode in urls)) {
            if ('clip' in urls) this.view_mode = 'clip';
            else if ('snap' in urls) this.view_mode = 'snap';
            else this.view_mode = 'tnb';
        }
    }

    _isVideoMode() {
        return this.view_mode == 'clip';
    }

    _isResourceSelected(index) {
        return this.currentResourceIndex == index;
    }

    _getResource(index) {
        if (this.resources !== undefined && index !== undefined && this.resources.length > 0) {
            return this.resources[index];
        }
        else {
            return {
                urls: {},
                caption: "No images or videos to display"
            };
        }
    }

    _currentResource() {
        return this._getResource(this.currentResourceIndex);
    }

    _currentResourceUrl() {
        return this._currentResource().urls[this.view_mode];
    }

    _currentResourceFilename() {
        return this._currentResource().date + '_' + this._currentResource().name;
    }

    _currentResourceFullTitle() {
        let title = (this.config.name === undefined) ? this._currentResource().streamgl : this.config.name
        return title + " - " + this._currentResource().trigger + " - " + this._currentResource().caption
    }

    _pad2(number) {
        return (number < 10 ? '0' : '') + number
    }

    _dateChanged(ev) {
        this.date_input = new Date(ev.target.value);
        this.time_input = undefined;
        this._loadResources(this._hass);
    }

    _reloadResources(hass) {
        this.date_input = new Date();
        this.time_input = undefined;
        this._loadResources(this._hass);
    }

    _loadResources(hass) {

        this.resources = [];
        this.currentResourceIndex = undefined;
        this.res_msg = "Loading...";
        this.requestUpdate();

        hass.callWS({
            type: "streamgl/gallery_list",
            streamgl: this.config.streamgl,
            date: this.date_input.toISOString(),
            triggers: (this.config.triggers === undefined) ? [] : this.config.triggers
        }).then(result => {
            if (this.config.reversed !== undefined && this.config.reversed) {
                result = [...result].reverse()
            }
            result.forEach(item => {
                let caption = item.time.substring(0, 2) + ":" + item.time.substring(2, 4) + ":" + item.time.substring(4, 6)
                this.resources.push({ ...item, caption: caption });
                if (this.currentResourceIndex == undefined && this.time_input == item.time) {
                    this.currentResourceIndex = this.resources.length - 1;
                }
            })
            this.res_msg = (this.resources.length == 0) ? "No Media" : "";
            this.requestUpdate();
        }, reject => {
            this.res_msg = reject;
            this.requestUpdate();
        });
    }

    static get styles() {
        return css`
      .content {
        overflow: hidden;
      }
      .content hui-card-preview {
        max-width: 100%;
      }
      ha-card {
        height: 100%;
        overflow: hidden;
      }
      .resource-viewer {
        position: relative;
      }
      .button-bar {
        position: absolute;
        top: 0%;
        right: 0%;
        padding: 2% 5%;
      }
      .btn-mode {
        color: white;
        background-color: transparent;
        border: none;
        cursor: pointer;
      }
      .btn-mode-selected {
        color: #00CCFF;
        background-color: transparent;
        border: none;
        cursor: pointer;
      }
      figure {
        margin: 0px;
        padding: 0px;
        width: 100%;
        height: 100%;
        object-fit: contain;
      }
      img, video {
        width: 100%;
        object-fit: contain;
      }
      .title {
        position: absolute;
        width: 100%;
        top: 0;
        background-color: rgba(0, 0, 0, 0.3);
        color: white;
        padding: 2% 5%;
        box-sizing: border-box;
      }
      .resource-viewer .title {
        font-size: 16px;
      }
      .resource-viewer .btn {
        position: absolute;
        top: 45%;
        background-color: #555;
        background-color: transparent;
        color: #00CCFF;
        border: none;
        cursor: pointer;
        opacity: 0;
        transition: opacity .35s ease;
      }
      .resource-viewer:hover .btn {
        opacity: 1;
      }
      .resource-viewer .btn-left {
        left: 0%;
        margin-left: 5%;
      }
      .resource-viewer .btn-right {
        right: 0%;
        margin-right: 5%;
      }
      .btn-reload {
        background-color: transparent;
        border: none;
        cursor: pointer;
      }
      .btn-live {
        border-width: 1%;
        border-style: solid;
        border-color: transparent;
        cursor: pointer;
      }
      .btn-live-selected {
        border-width: 1%;
        border-style: solid;
        color: #00CCFF;
        border-color: #00CCFF;
      }
      .date-input {
        float: right;
        background-color: transparent;
        border: none;
        cursor: pointer;
        margin: 0 auto;
      }
      .btn-date {
        background-color: transparent;
        border: none;
        cursor: pointer;
      }
      .resource-menu {
        position: relative;
      }
      .resource-menu .title {
        font-size: 10px;
      }
      .menu-bottom .resource-viewer {
        width: 100%;
      }
      .menu-bottom .resource-menu {
        width: 100%;
        overflow-y: hidden;
        overflow-x: scroll;
        display: flex;
        align-items: stretch;
      }
      .menu-bottom .resource-menu .resource-item {
        position: relative;
        width: 38%;
        border-width: 1%;
        border-style: solid;
        border-color: transparent;
        flex-shrink: 0;
      }
      .menu-bottom .resource-menu .resource-item-selected {
        position: relative;
        width: 38%;
        border-width: 1%;
        border-style: solid;
        border-color: #00CCFF;
        flex-shrink: 0;
      }
    `;
    }
}
customElements.define("streamgl-gallery", StreaMGLGalleryCard);

const card = {
    type: 'streamgl-gallery',
    name: 'StreaMGL Gallery',
    preview: false,
    description: 'StreaMGL Gallery allows to navigate a gallery of clip and snap',
};

if (window.customCards) window.customCards.push(card);
else window.customCards = [card];
